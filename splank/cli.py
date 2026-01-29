"""Splank CLI - Main entry point."""

import argparse
import csv
import fnmatch
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from toon_format import encode as toon_encode
from typing import Iterator

from splank import __version__
from splank.config import get_client, init_config

# Internal Splunk fields to drop by default (keep _time and _raw)
INTERNAL_FIELDS = {"_bkt", "_cd", "_indextime", "_serial", "_sourcetype", "_subsecond"}
# Prefixes for array-style fields like _si[0], _si[1], etc.
INTERNAL_PREFIXES = ("_si",)


def truncate_fields(row: dict, width: int | None) -> dict:
    """Truncate all string fields to width if needed."""
    if not width:
        return row
    result = {}
    for k, v in row.items():
        s = str(v)
        result[k] = s[:width] + "..." if len(s) > width else v
    return result


def filter_internal_fields(row: dict) -> dict:
    """Remove internal Splunk fields from a row."""
    return {
        k: v
        for k, v in row.items()
        if k not in INTERNAL_FIELDS and not k.startswith(INTERNAL_PREFIXES)
    }


def output_json(results: list[dict], file: str | None = None) -> None:
    """Output results as JSON."""
    output = json.dumps(results, indent=2)
    if file:
        with open(file, "w") as f:
            f.write(output)
    else:
        print(output)


def output_toon(results: list[dict], file: str | None = None) -> None:
    """Output results as TOON (Token-Oriented Object Notation)."""
    output = toon_encode(results)
    if file:
        with open(file, "w") as f:
            f.write(output)
    else:
        print(output)


def output_csv(results: list[dict], file: str | None = None) -> None:
    """Output results as CSV."""
    if not results:
        return

    # Get all unique fields
    fields: set[str] = set()
    for row in results:
        fields.update(row.keys())
    sorted_fields = sorted(fields)

    if file:
        f = open(file, "w", newline="")
    else:
        f = sys.stdout

    writer = csv.DictWriter(f, fieldnames=sorted_fields)
    writer.writeheader()
    writer.writerows(results)

    if file:
        f.close()


def output_table_streaming(results_iter: Iterator[dict]) -> None:
    """Output results as a simple table, streaming rows as they arrive."""
    fields: list[str] | None = None
    widths: dict[str, int] | None = None
    buffer: list[dict] = []
    header_printed = False

    for row in results_iter:
        if fields is None:
            # First row - determine fields from it
            fields = sorted(row.keys())
            widths = {f: max(len(f), 10) for f in fields}

        # Update widths and buffer until we have enough to print header
        if not header_printed:
            buffer.append(row)
            for f in fields:
                val = str(row.get(f, ""))
                widths[f] = max(widths[f], min(len(val), 50))

            # Print header after first few rows to get better column widths
            if len(buffer) >= 5:
                header = " | ".join(
                    f.ljust(widths[f])[: widths[f]] for f in fields
                )
                print(header)
                print("-" * len(header))
                header_printed = True
                for buffered_row in buffer:
                    line = " | ".join(
                        str(buffered_row.get(f, "")).ljust(widths[f])[: widths[f]]
                        for f in fields
                    )
                    print(line)
                buffer = []
        else:
            # Stream directly
            line = " | ".join(
                str(row.get(f, "")).ljust(widths[f])[: widths[f]] for f in fields
            )
            print(line)

    # Print any remaining buffered rows
    if buffer:
        if fields and widths:
            header = " | ".join(
                f.ljust(widths[f])[: widths[f]] for f in fields
            )
            print(header)
            print("-" * len(header))
            for row in buffer:
                line = " | ".join(
                    str(row.get(f, "")).ljust(widths[f])[: widths[f]] for f in fields
                )
                print(line)
    elif fields is None:
        print("No results")


def get_single_profile(args: argparse.Namespace) -> str | None:
    """Get a single profile from args (for commands that don't support multi-profile)."""
    if args.profiles:
        return args.profiles[0]
    return None


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize credentials file."""
    init_config()


def _search_one_profile(
    profile: str | None,
    query: str,
    earliest: str,
    latest: str,
    max_results: int,
) -> tuple[str | None, list[dict]]:
    """Execute search for a single profile, returning (profile, results)."""
    client = get_client(profile)
    results = list(
        client.search(
            query=query,
            earliest=earliest,
            latest=latest,
            max_results=max_results,
            stream=False,
        )
    )
    return profile, results


def cmd_search(args: argparse.Namespace) -> None:
    """Execute a search query."""
    profiles = args.profiles or [None]  # None = use default profile
    use_streaming = args.format == "table" and not args.output and len(profiles) == 1

    # Apply field transformations
    def transform(row: dict, profile: str | None = None) -> dict:
        if profile is not None and len(profiles) > 1:
            row = {"_profile": profile, **row}
        if not args.internal:
            row = filter_internal_fields(row)
        if args.width:
            row = truncate_fields(row, args.width)
        return row

    # Single profile: use streaming if appropriate
    if len(profiles) == 1:
        client = get_client(profiles[0])
        results = client.search(
            query=args.query,
            earliest=args.earliest,
            latest=args.latest,
            max_results=args.max_results,
            stream=use_streaming,
        )

        # Handle --zoom: extract JSON from _raw
        if args.zoom:
            zoomed = []
            for row in results:
                raw = str(row.get("_raw", "")).strip()
                if raw.startswith("{"):
                    try:
                        zoomed.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass  # Skip non-JSON rows
            output_toon(zoomed, args.output)
            return

        if use_streaming:
            results = (transform(row) for row in results)
        else:
            results = [transform(row) for row in results]
    else:
        # Multiple profiles: run in parallel
        all_results: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
            futures = {
                executor.submit(
                    _search_one_profile,
                    p,
                    args.query,
                    args.earliest,
                    args.latest,
                    args.max_results,
                ): p
                for p in profiles
            }
            for future in as_completed(futures):
                profile, profile_results = future.result()
                for row in profile_results:
                    all_results.append(transform(row, profile))

        # Handle --zoom: extract JSON from _raw
        if args.zoom:
            zoomed = []
            for row in all_results:
                raw = str(row.get("_raw", "")).strip()
                if raw.startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        if "_profile" in row:
                            parsed = {"_profile": row["_profile"], **parsed}
                        zoomed.append(parsed)
                    except json.JSONDecodeError:
                        pass
            output_toon(zoomed, args.output)
            return

        results = all_results

    if args.format == "json":
        output_json(list(results), args.output)
    elif args.format == "csv":
        output_csv(list(results), args.output)
    elif args.format == "toon":
        output_toon(list(results), args.output)
    else:
        output_table_streaming(results)


def cmd_clear(args: argparse.Namespace) -> None:
    """Clear search jobs to free quota."""
    client = get_client(get_single_profile(args))

    # List user's jobs
    jobs = client.list_jobs(count=100)

    # Filter to non-scheduler jobs (user's own jobs)
    my_jobs = [
        j for j in jobs if not j["content"].get("sid", "").startswith("scheduler_")
    ]

    if not my_jobs:
        print("No jobs to clear.")
        return

    deleted = 0
    for job in my_jobs:
        sid = job["content"]["sid"]
        try:
            client.delete_job(sid)
            deleted += 1
        except Exception:
            pass

    print(f"Cleared {deleted} job(s).")


def cmd_discover(args: argparse.Namespace) -> None:
    """Discover available indexes via search."""
    client = get_client(get_single_profile(args))

    # Use eventcount to discover all searchable indexes (including federated)
    print("Discovering indexes...", file=sys.stderr)
    results = list(
        client.search(
            "| eventcount summarize=false index=*", earliest="-24h", max_results=1000
        )
    )

    # Dedupe and sum counts across indexers
    index_counts: dict[str, int] = {}
    for row in results:
        name = row.get("index", "")
        count = int(row.get("count", 0))
        index_counts[name] = max(index_counts.get(name, 0), count)

    # Filter indexes
    indexes: list[tuple[str, int]] = []
    for name in sorted(index_counts.keys()):
        if not args.all and name.startswith(("_", "history", "summary")):
            continue
        if args.patterns:
            if not any(fnmatch.fnmatch(name, p) for p in args.patterns):
                continue
        indexes.append((name, index_counts[name]))

    if not args.fields:
        # Simple list mode
        for name, count in indexes:
            print(f"{name:40} {count:>12} events")
        return

    # Detailed mode with fields - output markdown
    output = ["# Splunk Index Discovery", ""]

    for name, count in indexes:
        print(f"Discovering fields for {name}...", file=sys.stderr)
        output.append(f"## {name}")
        output.append(f"- **Events (24h):** {count:,}")

        # Get sourcetypes
        try:
            st_results = list(
                client.search(
                    f"index={name} | stats count by sourcetype | sort -count",
                    earliest="-24h",
                    max_results=20,
                )
            )
            if st_results:
                sourcetypes = [r.get("sourcetype", "") for r in st_results]
                output.append(f"- **Sourcetypes:** {', '.join(sourcetypes)}")
        except Exception:
            pass

        # Get fields using fieldsummary
        try:
            field_results = list(
                client.search(
                    f"index={name} | head 1000 | fieldsummary | where count > 100 | sort -count | head 30",
                    earliest="-24h",
                    max_results=50,
                )
            )
            if field_results:
                output.append("")
                output.append("### Fields")
                output.append("")
                # Skip internal fields
                skip_fields = {
                    "date_hour",
                    "date_mday",
                    "date_minute",
                    "date_month",
                    "date_second",
                    "date_wday",
                    "date_year",
                    "date_zone",
                    "punct",
                    "timestartpos",
                    "timeendpos",
                    "linecount",
                    "index",
                    "splunk_server",
                }
                fields = [
                    f.get("field", "")
                    for f in field_results
                    if f.get("field", "") not in skip_fields
                ]
                output.append(", ".join(f"`{f}`" for f in fields))

                # Get sample values for interesting fields
                interesting = {
                    "Level",
                    "level",
                    "severity",
                    "status",
                    "sourcetype",
                    "host",
                    "TenantType",
                    "Environment",
                    "environment",
                    "env",
                    "Region",
                    "region",
                    "cluster",
                    "Cluster",
                    "cluster_name",
                    "ClusterName",
                }
                found_interesting = [f for f in fields if f in interesting]
                if found_interesting:
                    output.append("")
                    output.append("### Sample Values")
                    output.append("")
                    # Get a few sample events
                    try:
                        sample_events = list(
                            client.search(
                                f"index={name} | head 100",
                                earliest="-1h",
                                max_results=100,
                            )
                        )
                        for field in found_interesting:
                            seen: set[str] = set()
                            for evt in sample_events:
                                val = str(evt.get(field, "")).strip()[:50]
                                if val:
                                    seen.add(val)
                                if len(seen) >= 5:
                                    break
                            if seen:
                                output.append(
                                    f"- **{field}:** {', '.join(f'`{s}`' for s in sorted(seen))}"
                                )
                    except Exception:
                        pass
        except Exception as e:
            output.append(f"*Error getting fields: {e}*")

        output.append("")

    # Write output
    md_content = "\n".join(output)
    if args.output:
        with open(args.output, "w") as f:
            f.write(md_content)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(md_content)


def cmd_jobs(args: argparse.Namespace) -> None:
    """List search jobs."""
    client = get_client(get_single_profile(args))

    jobs = client.list_jobs(count=50)

    if args.mine:
        jobs = [
            j for j in jobs if not j["content"].get("sid", "").startswith("scheduler_")
        ]

    if not jobs:
        print("No jobs found.")
        return

    total_mb = 0.0
    for job in jobs:
        content = job["content"]
        state = content.get("dispatchState", "?")
        sid = content.get("sid", "?")
        disk = content.get("diskUsage", 0)
        total_mb += disk / 1024 / 1024
        search = content.get("search", "")[:50]
        print(f"{state:10} {disk/1024/1024:6.1f}MB  {sid[:25]:25}  {search}")

    print(f"\nTotal: {total_mb:.1f}MB")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="splank",
        description="CLI tool for querying Splunk logs",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-p",
        "--profile",
        action="append",
        dest="profiles",
        metavar="PROFILE",
        help="Splunk profile to use (repeatable for parallel search)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize credentials")
    init_parser.set_defaults(func=cmd_init)

    # search command
    search_parser = subparsers.add_parser("search", help="Execute SPL query")
    search_parser.add_argument("query", help="SPL query to execute")
    search_parser.add_argument(
        "--earliest", "-e", default="-24h", help="Earliest time (default: -24h)"
    )
    search_parser.add_argument(
        "--latest", "-l", default="now", help="Latest time (default: now)"
    )
    search_parser.add_argument(
        "--max-results", "-m", type=int, default=100, help="Max results (default: 100)"
    )
    search_parser.add_argument(
        "--format",
        "-f",
        choices=["json", "csv", "table", "toon"],
        default="toon",
        help="Output format (default: toon)",
    )
    search_parser.add_argument(
        "--output", "-o", help="Output file (default: stdout)"
    )
    search_parser.add_argument(
        "--internal",
        action="store_true",
        help="Include internal Splunk fields (_bkt, _cd, etc.)",
    )
    search_parser.add_argument(
        "--width",
        "-w",
        type=int,
        default=500,
        metavar="N",
        help="Truncate field values to N chars (default: 500, 0=no limit)",
    )
    search_parser.add_argument(
        "--zoom",
        "-z",
        action="store_true",
        help="Parse JSON from _raw and output as toon (ignores other fields)",
    )
    search_parser.set_defaults(func=cmd_search)

    # discover command
    discover_parser = subparsers.add_parser(
        "discover", help="Discover available indexes"
    )
    discover_parser.add_argument(
        "patterns",
        nargs="*",
        help="Glob patterns to filter indexes (e.g. 'web*' 'app-*')",
    )
    discover_parser.add_argument(
        "--all", "-a", action="store_true", help="Include internal indexes"
    )
    discover_parser.add_argument(
        "--fields", "-f", action="store_true", help="Discover fields for each index"
    )
    discover_parser.add_argument(
        "--output", "-o", help="Output file (default: stdout)"
    )
    discover_parser.set_defaults(func=cmd_discover)

    # jobs command
    jobs_parser = subparsers.add_parser("jobs", help="List search jobs")
    jobs_parser.add_argument(
        "--mine", action="store_true", help="Show only my jobs"
    )
    jobs_parser.set_defaults(func=cmd_jobs)

    # clear command
    clear_parser = subparsers.add_parser("clear", help="Clear my search jobs")
    clear_parser.set_defaults(func=cmd_clear)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
