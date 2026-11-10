"""Read-only adapters for the Atlas Drive queue and Europe PMC."""

import json
import hashlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from preflight import PDF_NAME

EUROPE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DRIVE = "https://www.googleapis.com/drive/v3/files"
DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"?#]+", re.I)
ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
MAX_SOURCE = 160_000
OPENAI_LIMIT_CODES = {
    "credit_balance_exhausted", "insufficient_quota", "organization_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "project_spend_limit_exceeded", "rate_limit_exceeded",
    "rate_limit_error", "slow_down", "tokens", "requests",
}


class Unavailable(Exception):
    """A remote request failed. Messages contain only safe diagnostic codes."""


class Review(Exception):
    """An item needs human review before processing can continue."""


def get(url, params=None, headers=None, limit=4_000_000, body=None):
    if params:
        url += "?" + urlencode(params)
    request = Request(url, data=body)
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            request.add_unredirected_header(key, value)
        else:
            request.add_header(key, value)
    # Generation POSTs must be reserved and retried by the caller, once per run.
    attempts = 1 if body is not None else 3
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=240 if body is not None else 30) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise Review("remote_source_too_large")
                return data
        except HTTPError as error:
            code = error.code
            delay = error.headers.get("Retry-After", "")
            if code == 429 and body is not None and request.full_url == "https://api.openai.com/v1/responses":
                try:
                    problem = json.loads(error.read(4096))["error"]
                    detail = next((problem.get(field) for field in ("code", "type")
                                   if isinstance(problem.get(field), str)
                                   and problem[field] in OPENAI_LIMIT_CODES), None)
                except (ValueError, KeyError, TypeError, AttributeError, OSError):
                    detail = None
                # Keep only known codes, never the provider's response or message.
                if detail:
                    raise Unavailable(f"openai_{detail}") from None
            if code not in (408, 429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise Unavailable(f"http_{code}") from None
            time.sleep(min(30, int(delay)) if delay.isdecimal() else 2**attempt)
        except (URLError, TimeoutError, OSError):
            if attempt + 1 == attempts:
                raise Unavailable("connection_failure") from None
            time.sleep(2**attempt)


def doi(value):
    match = DOI.search(unescape(value or ""))
    return match[0].lower().rstrip(".,;") if match else ""


def pmid(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9]{1,12}", value) else ""


def clean(text):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", text or "")).split())


def title_key(title):
    return " ".join(re.findall(r"\w+", clean(title).casefold()))


@dataclass(frozen=True)
class Record:
    doi: str
    pmid: str
    title: str
    citation: str
    pmcid: str
    abstract: str
    open_access: bool
    publication_types: str
    revision: str = ""
    published: str = ""

    @classmethod
    def from_result(cls, hit):
        types = hit.get("pubTypeList", {}).get("pubType", [])
        corrections = hit.get("commentCorrectionList", {}).get("commentCorrection", [])
        status = " ".join([*types, *(row.get("type", "") for row in corrections)]).lower()
        info = hit.get("journalInfo") or {}
        journal = (info.get("journal") or {}).get("title") or hit.get("journalTitle") or ""
        title = clean(hit.get("title"))
        return cls(doi(hit.get("doi")), pmid(hit.get("pmid") or (
            hit.get("id") if hit.get("source") == "MED" else ""
        )), title, f"{hit.get('authorString', '')} {title} {journal} {hit.get('pubYear', '')}.".strip(),
            hit.get("pmcid") or "", clean(hit.get("abstractText")),
            hit.get("isOpenAccess") == "Y", status, hit.get("dateOfRevision") or "",
            hit.get("firstPublicationDate") or hit.get("pubYear") or "")

    @property
    def keys(self):
        return {value for value in (self.doi, self.pmid) if value}

    @property
    def url(self):
        return (f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/" if self.pmid
                else f"https://doi.org/{self.doi}")

    @property
    def excluded(self):
        return any(word in self.publication_types for word in (
            "retract", "expression of concern", "preprint", "published erratum",
        ))


class Literature:
    def search(self, query, size):
        response = json.loads(get(f"{EUROPE}/search", {
            "query": query, "resultType": "core", "format": "json", "pageSize": size,
        }))
        return [Record.from_result(hit) for hit in response["resultList"]["result"]]

    def identify(self, paper_doi, paper_pmid, title):
        if not paper_doi and not paper_pmid and not title_key(title):
            raise Review("missing_bibliographic_identity")
        query = (
            f"EXT_ID:{paper_pmid} AND SRC:MED" if paper_pmid else
            f'DOI:"{paper_doi}"' if paper_doi else
            'TITLE:"' + title_key(title)[:350] + '" AND SRC:MED'
        )
        records = self.search(query, 12)
        matches = [r for r in records if r.keys and not r.excluded
                   and (not paper_doi or paper_doi == r.doi)
                   and (not paper_pmid or paper_pmid == r.pmid)
                   and (paper_doi or paper_pmid or title_key(r.title) == title_key(title))]
        if len({r.pmid or r.doi for r in matches}) != 1:
            raise Review("unconfirmed_or_excluded_identity")
        return matches[0]

    def discover(self, query, today, days, size):
        since = today - timedelta(days=days)
        return self.search(
            f"({query}) AND SRC:MED AND HAS_ABSTRACT:Y "
            f"AND FIRST_PDATE:[{since} TO {today}] SORT_DATE:y", size,
        )

    def source(self, record):
        if record.open_access and re.fullmatch(r"PMC\d+", record.pmcid):
            url = f"{EUROPE}/{record.pmcid}/fullTextXML"
            try:
                article = ET.fromstring(get(url))
            except Unavailable as error:
                if str(error) not in ("http_404", "http_410"):
                    raise
            else:
                if article.get("article-type") in ("retraction", "correction", "expression-of-concern"):
                    raise Review("excluded_full_text_type")
                ids = {node.get("pub-id-type"): "".join(node.itertext()).strip()
                       for node in article.findall("./front/article-meta/article-id")}
                if ((record.pmid and ids.get("pmid") and ids["pmid"] != record.pmid)
                        or (record.doi and ids.get("doi") and doi(ids["doi"]) != record.doi)
                        or not ((record.pmid and ids.get("pmid") == record.pmid)
                                or (record.doi and doi(ids.get("doi")) == record.doi))):
                    raise Review("full_text_identity_mismatch")
                body = article.find("./body")
                if body is not None:
                    # Keep section and table boundaries. Preserve the entire body.
                    text = "\n".join(" ".join(node.itertext()) for node in body)
                    front = article.find("./front/article-meta")
                    notes = [] if front is None else [
                        " ".join(node.itertext()) for name in ("funding-group", "author-notes")
                        for node in front.findall(name)
                    ]
                    back = article.find("./back")
                    appendix = [] if back is None else [
                        " ".join(node.itertext()) for node in back if node.tag != "ref-list"
                    ]
                    text = "\n".join((record.title, text, *notes, *appendix))
                    if sufficient_full_text(text):
                        return "full_text", url, text
        if len(record.abstract.split()) >= 100:
            return "abstract", record.url, record.abstract
        raise Review("insufficient_source")


def sufficient_full_text(text):
    return (len(text.split()) >= 450
            and bool(re.search(r"\bmethods?\b", text, re.I))
            and bool(re.search(r"\bresults?\b", text, re.I)))


def extract_pdf(data):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "paper.pdf"
        path.write_bytes(data)
        try:
            result = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", "-layout", str(path), "-"],
                capture_output=True, timeout=40, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise Review("pdf_extraction_failed") from None
    return result.stdout.decode("utf-8")


class Drive:
    def __init__(self, service_account, folder):
        if not ID.fullmatch(folder):
            raise ValueError("invalid_folder_id")
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account as accounts
        creds = accounts.Credentials.from_service_account_info(
            json.loads(service_account), scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        creds.refresh(GoogleRequest())
        self.headers = {"Authorization": f"Bearer {creds.token}"}
        self.folder = folder
        metadata = json.loads(get(f"{DRIVE}/{folder}", {
            "fields": "id,mimeType,trashed", "supportsAllDrives": "true",
        }, self.headers))
        if (metadata.get("id") != folder or metadata.get("trashed")
                or metadata.get("mimeType") != "application/vnd.google-apps.folder"):
            raise Unavailable("atlas_drive_folder_unavailable")

    def entries(self):
        params = {
            "q": f"'{self.folder}' in parents and trashed = false",
            "fields": "nextPageToken,incompleteSearch,files(id,name,version,md5Checksum,size)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        files = []
        while True:
            response = json.loads(get(DRIVE, params, self.headers))
            if response.get("incompleteSearch"):
                raise Unavailable("incomplete_drive_listing")
            files.extend(response["files"])
            if not response.get("nextPageToken"):
                return sorted(files, key=lambda f: (not bool(PDF_NAME.fullmatch(f["name"])), f["name"]))
            params["pageToken"] = response["nextPageToken"]

    def download(self, entry, limit):
        if not ID.fullmatch(entry["id"]) or int(entry.get("size", 0)) > limit:
            raise Review("invalid_or_oversized_drive_file")
        data = get(f"{DRIVE}/{entry['id']}", {"alt": "media", "supportsAllDrives": "true"},
                   self.headers, limit)
        # Refuse content that changed between listing and download.
        if entry.get("md5Checksum") != hashlib.md5(data, usedforsecurity=False).hexdigest():
            raise Unavailable("drive_file_changed_or_checksum_missing")
        return data
