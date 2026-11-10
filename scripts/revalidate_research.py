"""Check retained readings offline. Does not call APIs, edit state or release holds."""

import argparse
import json
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path

from research_appraisal import appraisal_checks
from research_sources import Review, article_text


def revalidate(job, article):
    source = job["source"]
    if sha256(article.encode()).hexdigest() != source["text_sha256"]:
        return {"status": "source_hash_mismatch"}
    report = {}
    for phase, failure in job.get("validation_failures", {}).items():
        result = failure.get("reading")
        if result is None:
            report[phase] = {"status": "no_structured_reading", "reason": failure["reason"]}
            continue
        try:
            appraisal_checks(result, article, source["level"])
        except Review as error:
            report[phase] = {"status": "held", "reason": str(error), "fields": error.fields}
        else:
            report[phase] = {"status": "checks_passed"}
    return report or {"status": "reading_not_retained"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("job")
    parser.add_argument("source", type=Path, help="Extracted UTF-8 source text or original article XML")
    args = parser.parse_args()
    try:
        job = json.loads(args.state.read_bytes())["jobs"][args.job]
        source = (article_text(ET.parse(args.source).getroot(), job["record"]["title"])
                  if args.source.suffix.lower() == ".xml" else args.source.read_bytes().decode("utf-8"))
        report = revalidate(job, source)
    except (OSError, ValueError, KeyError, ET.ParseError):
        parser.exit(1, "Atlas could not read the local state, job or source.\n")
    print(json.dumps(report, indent=2))
