#!/usr/bin/env python3
"""Harvest metadata of open-access PubMed Central articles matching a query.

Why two APIs?
  The PMC OAI-PMH service (Open Archives Initiative Protocol for Metadata
  Harvesting) can only filter by set / date range / identifier -- it has no
  keyword search. So we
    1. SEARCH with NCBI E-utilities (ESearch) -> list of PMC IDs, then
    2. FETCH each record's metadata via OAI-PMH GetRecord (metadataPrefix=oai_dc)
       and keep only records that belong to set `pmc-open`, i.e. those whose
       full text we may retrieve later.

Output:  <out>/metadata.json   (+ empty <out>/pdf and <out>/txt for a later script)
Only the Python standard library is used.
"""
import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
OAI = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
OAI_ID_PREFIX = "oai:pubmedcentral.nih.gov:"
OAI_SET = "pmc-open"

# Any of these may match; a trailing * is a wildcard (PMC truncation).
DEFAULT_TERMS = [
    "PTS", "PTSS", "PTSD", "Posttraumatic stress", "CPTS*",
    "Psychological injur*", "Trauma*", "Impact of Event Scale",
]

# XML namespaces used in OAI-PMH responses with Dublin Core payloads.
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
# NCBI allows 3 requests/second and forbids concurrent requests.
MIN_DELAY = 0.34
_last_request = 0.0


def get(url, retries=4):
    """GET a URL politely: rate-limited, gzip-enabled, retried on 429/5xx."""
    global _last_request
    req = urllib.request.Request(url, headers={
        "User-Agent": "pmc-educational-harvester/0.1",
        "Accept-Encoding": "gzip, deflate",
    })
    for attempt in range(retries):
        time.sleep(max(0.0, MIN_DELAY - (time.time() - _last_request)))
        _last_request = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))  # back off: 2s, 4s, 8s
                continue
            raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"giving up on {url}")


def build_query(terms):
    """Turn terms into an Entrez query: each term searched in title/abstract."""
    parts = []
    for t in terms:
        # Quote exact terms/phrases. Wildcard terms stay unquoted (Entrez
        # disables truncation inside quotes); NCBI normalises them itself.
        parts.append(f"{t}[tiab]" if "*" in t else f'"{t}"[tiab]')
    # "open access"[filter] pre-filters to the OA subset (we re-check via OAI).
    return "(" + " OR ".join(parts) + ') AND "open access"[filter]'


def esearch(query, max_results, email=None):
    """Return a list of numeric PMC IDs (as strings) matching the query."""
    ids, retstart, page = [], 0, 1000
    while True:
        want = page if not max_results else min(page, max_results - len(ids))
        params = {"db": "pmc", "term": query, "retmode": "json",
                  "retstart": retstart, "retmax": want, "tool": "pmc-harvester"}
        if email:
            params["email"] = email
        if os.environ.get("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        result = json.loads(get(ESEARCH + "?" + urllib.parse.urlencode(params)))["esearchresult"]
        if retstart == 0:
            print(f"NCBI interpreted the query as:\n  {result.get('querytranslation')}")
            print(f"Total matches: {result['count']}")
        batch = result["idlist"]
        ids += batch
        retstart += len(batch)
        if not batch or retstart >= int(result["count"]) or (max_results and len(ids) >= max_results):
            return ids


def oai_get_record(pmc_numeric_id):
    """Fetch Dublin Core metadata for one article. None if not in pmc-open."""
    identifier = OAI_ID_PREFIX + pmc_numeric_id
    url = OAI + "?" + urllib.parse.urlencode(
        {"verb": "GetRecord", "identifier": identifier, "metadataPrefix": "oai_dc"})
    root = ET.fromstring(get(url))
    if root.find("oai:error", NS) is not None:  # e.g. idDoesNotExist
        return None
    sets = [s.text for s in root.findall(".//oai:header/oai:setSpec", NS)]
    if OAI_SET not in sets:
        return None  # full text not available for bulk retrieval

    def all_(tag):
        return [e.text for e in root.findall(f".//dc:{tag}", NS) if e.text]

    def first(tag):
        vals = all_(tag)
        return vals[0] if vals else None

    return {
        "pmcid": "PMC" + pmc_numeric_id,
        "oai_identifier": identifier,
        "sets": sets,
        "title": first("title"),
        "creators": all_("creator"),
        "subjects": all_("subject"),
        "description": first("description"),
        "publisher": first("publisher"),
        "date": first("date"),
        "type": all_("type"),
        "identifiers": all_("identifier"),
        "source": first("source"),
        "rights": all_("rights"),
    }


def save(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # atomic: never leaves a half-written file


def main():
    ap = argparse.ArgumentParser(description="Harvest PMC open-access metadata.")
    ap.add_argument("--out", default="data", help="output folder (default: ./data)")
    ap.add_argument("--max-results", type=int, default=100,
                    help="max articles to fetch; 0 = all (default: 100)")
    ap.add_argument("--terms", nargs="+", default=DEFAULT_TERMS,
                    help="query terms, any may match; * is a wildcard")
    ap.add_argument("--email", help="contact email sent to NCBI (recommended)")
    args = ap.parse_args()

    for sub in ("", "pdf", "txt"):
        os.makedirs(os.path.join(args.out, sub), exist_ok=True)
    meta_path = os.path.join(args.out, "metadata.json")

    # Resume support: keep what we already have.
    records = []
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            records = json.load(f)
    done = {r["pmcid"] for r in records}

    query = build_query(args.terms)
    print(f"Query: {query}")
    ids = esearch(query, args.max_results, args.email)
    todo = [i for i in ids if "PMC" + i not in done]
    print(f"{len(ids)} hits, {len(todo)} new. (NCBI asks to run >100 requests "
          f"outside Mon-Fri 5am-9pm US Eastern.)")

    skipped = 0
    for n, pmc_id in enumerate(todo, 1):
        rec = oai_get_record(pmc_id)
        if rec is None:
            skipped += 1
        else:
            records.append(rec)
        if n % 25 == 0 or n == len(todo):
            save(meta_path, records)
            print(f"  {n}/{len(todo)} processed, {len(records)} stored, {skipped} not in {OAI_SET}")
    save(meta_path, records)
    print(f"Done. {len(records)} records in {meta_path}")


if __name__ == "__main__":
    main()
