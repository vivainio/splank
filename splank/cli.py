"""Splank CLI - Main entry point."""

import argparse
import csv
import fnmatch
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlparse

from splank._vendor.toon_format import encode as toon_encode
from splank.o11y_client import (
    apm_environments,
    build_tool_arguments,
    compare_windows,
    describe_fields,
    exemplar_traces,
    parse_duration_ms,
    parse_time_ms,
    parse_window,
    resolve_tool,
    tool_inputs,
)
from typing import Iterator

from splank import __version__
from splank.config import (
    add_profile_wizard,
    get_client,
    get_mcp_client,
    get_o11y_client,
    get_o11y_mcp_client,
    init_config,
    resolve_profile_name,
    set_mcp_config,
    set_o11y_config,
)

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


_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def sanitize_fields(row: dict) -> dict:
    """Mask email addresses in all string field values."""
    result = {}
    for k, v in row.items():
        if isinstance(v, str):
            result[k] = _EMAIL_RE.sub("***@***.***", v)
        else:
            result[k] = v
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
    """Initialize credentials file, or add a single profile via a short wizard."""
    if args.name:
        add_profile_wizard(name=args.name, host=args.host, mcp=args.mcp)
    else:
        init_config()


def _search_one_profile(
    profile: str | None,
    query: str,
    earliest: str,
    latest: str,
    max_results: int,
    mcp: bool | None = None,
) -> tuple[str | None, list[dict]]:
    """Execute search for a single profile, returning (profile, results)."""
    client = get_client(profile, mcp)
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


def parse_splunk_url(value: str) -> tuple[str, str | None, str | None] | None:
    """If value is a Splunk search URL, extract (query, earliest, latest).

    Returns None if value isn't a recognizable Splunk URL with a `q` param.
    """
    if not value.startswith(("http://", "https://")):
        return None
    parsed = urlparse(value)
    params = parse_qs(parsed.query)
    q_list = params.get("q")
    if not q_list:
        return None
    query = q_list[0]
    # Splunk URLs typically prefix the SPL with "search "; strip it so the
    # resulting query works whether the user later edits it or not.
    if query.startswith("search "):
        query = query[len("search "):]
    earliest = params.get("earliest", [None])[0]
    latest = params.get("latest", [None])[0]
    return query, earliest, latest


def cmd_search(args: argparse.Namespace) -> None:
    """Execute a search query."""
    parsed_url = parse_splunk_url(args.query)
    if parsed_url is not None:
        query, url_earliest, url_latest = parsed_url
        args.query = query
        # URL time range wins unless user explicitly overrode the default.
        if url_earliest and args.earliest == "-24h":
            args.earliest = url_earliest
        if url_latest and args.latest == "now":
            args.latest = url_latest
        print(
            f"Parsed URL: query={args.query!r} earliest={args.earliest} latest={args.latest}",
            file=sys.stderr,
        )

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
        if not args.no_sanitize:
            row = sanitize_fields(row)
        return row

    # Single profile: use streaming if appropriate
    if len(profiles) == 1:
        client = get_client(profiles[0], args.mcp)
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
                    args.mcp,
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
    client = get_client(get_single_profile(args), mcp=False)

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
    client = get_client(get_single_profile(args), args.mcp)

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
    client = get_client(get_single_profile(args), mcp=False)

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


def _extract_spl(result: dict) -> str:
    """Extract the generated SPL string from a saia_generate_spl tool result."""
    response = None

    structured = result.get("structuredContent")
    if structured:
        results = structured.get("results") or []
        if results:
            response = results[0].get("response")

    if response is None:
        for block in result.get("content") or []:
            if block.get("type") != "text":
                continue
            try:
                parsed = json.loads(block["text"])
                results = parsed.get("results") or []
                response = results[0].get("response") if results else block["text"]
            except json.JSONDecodeError:
                response = block["text"]
            break

    if isinstance(response, list):
        response = "\n".join(response)
    if not response:
        raise RuntimeError("Could not extract SPL from saia_generate_spl response")

    # Strip a leading/trailing markdown code fence if present. SAIA doesn't
    # always emit a matched ``` pair (sometimes only the opening fence), so
    # strip each end independently rather than requiring a full block match.
    lines = response.strip().splitlines()
    first_line = lines[0].strip() if lines else ""
    if first_line.startswith("```") or first_line.lower() == "splunk-spl":
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def cmd_ask(args: argparse.Namespace) -> None:
    """Generate SPL from a free-form question via the MCP server, then run it."""
    profile = get_single_profile(args)
    client = get_mcp_client(profile)
    result = client.call_tool(
        "saia_generate_spl", {"prompt": args.question, "spl_only": True}
    )
    spl = _extract_spl(result)

    print(f"Generated SPL: {spl}", file=sys.stderr)

    if args.dry_run:
        return

    args.query = spl
    cmd_search(args)


def cmd_o11y_set(args: argparse.Namespace) -> None:
    """Save Observability Cloud realm and token into a profile's TOML block."""
    name = resolve_profile_name(get_single_profile(args))

    realm = args.realm or input("Observability realm (e.g. eu0, us1): ").strip()
    if not realm:
        print("Realm is required.", file=sys.stderr)
        sys.exit(1)

    token = args.token or input("Observability API token: ").strip()
    if not token:
        print("Token is required.", file=sys.stderr)
        sys.exit(1)

    set_o11y_config(name, realm, token)
    print(f"Saved o11y_realm/o11y_token for profile '{name}'.")


def _output_o11y(results: list[dict], args: argparse.Namespace) -> None:
    """Print Observability results in the requested format."""
    if args.format == "json":
        output_json(results, args.output)
    elif args.format == "csv":
        output_csv(
            [{k: v if not isinstance(v, (dict, list)) else json.dumps(v) for k, v in r.items()} for r in results],
            args.output,
        )
    else:
        output_toon(results, args.output)


def cmd_o11y_query(args: argparse.Namespace) -> None:
    """Run a metadata lookup against Observability Cloud and print the results."""
    client = get_o11y_client(get_single_profile(args))
    if args.o11y_command == "metrics":
        results = client.metrics(args.query, args.limit)
    elif args.o11y_command == "mts":
        results = client.metric_time_series(args.query, args.limit)
    elif args.o11y_command == "detectors":
        results = client.detectors(args.name, args.limit)
    else:  # incidents
        results = client.incidents(
            args.resolved,
            args.limit,
            detector=args.detector,
            since_ms=parse_time_ms(args.since) if args.since else None,
            until_ms=parse_time_ms(args.until) if args.until else None,
        )
    _output_o11y(results, args)


def cmd_o11y_signalflow(args: argparse.Namespace) -> None:
    """Run a SignalFlow program and print the datapoints."""
    client = get_o11y_client(get_single_profile(args))
    start = parse_time_ms(args.earliest)
    stop = parse_time_ms(args.latest)
    resolution = parse_duration_ms(args.resolution) if args.resolution else None
    rows = []
    for row in client.signalflow(args.program, start, stop, resolution):
        rows.append(row)
        if len(rows) >= args.limit:
            break
    if not rows:
        print(
            "No datapoints. Does the program end with .publish()? Is the time range right?",
            file=sys.stderr,
        )
        return
    _output_o11y(rows, args)


def cmd_o11y_compare(args: argparse.Namespace) -> None:
    """Compare a SignalFlow metric between a before and an after window, per group."""
    import statistics

    client = get_o11y_client(get_single_profile(args))
    before = parse_window(*args.before)
    after = parse_window(*args.after)
    start = min(before[0], after[0])
    stop = min(max(before[1], after[1]), parse_time_ms("now"))
    resolution = parse_duration_ms(args.resolution)
    group_by = [g.strip() for g in (args.group_by or "").split(",") if g.strip()]
    focus = {f.strip().lower() for f in args.focus.split(",") if f.strip()} if args.focus else None

    rows = list(client.signalflow(args.program, start, stop, resolution))
    if not group_by and rows:
        # Default: group by every dimension the series carries.
        group_by = [k for k in rows[0] if k not in ("timestamp", "value", "label", "metric")]
    results = compare_windows(
        rows, group_by, before, after, args.stat, not args.include_weekends, args.min_points, focus
    )
    if not results:
        print(
            f"No groups with >= {args.min_points} datapoints in both windows "
            f"({len(rows)} datapoints fetched). Check the program, windows and --group-by.",
            file=sys.stderr,
        )
        return
    if focus is not None:
        for role in ("focus", "control"):
            pcts = [r["change_pct"] for r in results if r["role"] == role and r["change_pct"] is not None]
            if pcts:
                print(
                    f"{role}: {len(pcts)} groups, median change {statistics.median(pcts):+.1f}%",
                    file=sys.stderr,
                )
    _output_o11y(results, args)


def cmd_o11y_services(args: argparse.Namespace) -> None:
    """List APM services (or the dependency edges between them)."""
    client = get_o11y_client(get_single_profile(args))
    topo = client.services(parse_time_ms(args.earliest), parse_time_ms(args.latest))
    if args.edges:
        items = topo.get("edges", [])
        key = lambda r: f"{r.get('fromNode', '')} {r.get('toNode', '')}"
    else:
        items = topo.get("nodes", [])
        key = lambda r: r.get("serviceName", "")
        if args.type:
            items = [n for n in items if n.get("type") == args.type]
    if args.patterns:
        items = [r for r in items if any(fnmatch.fnmatch(key(r).lower(), p.lower()) for p in args.patterns)]
    _output_o11y(items[: args.limit], args)


def cmd_o11y_tools(args: argparse.Namespace) -> None:
    """List the tools the Observability MCP gateway exposes, or describe one."""
    mcp = get_o11y_mcp_client(get_single_profile(args), args.toolsets)
    tools = mcp.list_tools()

    if args.name:
        try:
            tool = resolve_tool(tools, args.name)
        except RuntimeError:
            tool = None
        if tool:  # exact match: show its description and input fields
            _, props, required = tool_inputs(tool)
            print(f"{tool['name']}\n")
            print((tool.get("description") or "(no description)").strip() + "\n")
            _output_o11y(describe_fields(props, required), args)
            return

    needle = (args.name or "").lower()
    rows = [
        {
            "tool": t["name"],
            "description": " ".join((t.get("description") or "").split())[:110],
        }
        for t in sorted(tools, key=lambda t: t["name"])
        if needle in t["name"].lower() or needle in (t.get("description") or "").lower()
    ]
    if not rows:
        print(f"No tools match {args.name!r}.", file=sys.stderr)
        return
    _output_o11y(rows, args)


def cmd_o11y_tool(args: argparse.Namespace) -> None:
    """Call any Observability MCP tool: key=value arguments, dotted keys nest."""
    mcp = get_o11y_mcp_client(get_single_profile(args), args.toolsets)
    tool = resolve_tool(mcp.list_tools(), args.tool)
    try:
        arguments = build_tool_arguments(tool, args.args, args.json_args)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    result = mcp.call_tool(tool["name"], arguments)
    texts = [b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text"]
    text = "\n".join(texts)
    if result.get("isError") or text.startswith("Invalid arguments"):
        _, props, required = tool_inputs(tool)
        print(f"Error from {tool['name']}: {text or 'tool reported an error'}", file=sys.stderr)
        print(f"Run 'splank oy tools {args.tool}' to see its fields.", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(text if text else json.dumps(result, indent=2))
        return
    if args.raw:
        print(text)
    elif args.format == "json":
        output_json(data, args.output)
    elif args.format == "csv":
        output_csv(data if isinstance(data, list) else [data], args.output)
    else:
        output_toon(data, args.output)


def cmd_o11y_envs(args: argparse.Namespace) -> None:
    """List APM environments (via the Observability MCP gateway)."""
    mcp = get_o11y_mcp_client(get_single_profile(args))
    _output_o11y([{"environment": e} for e in apm_environments(mcp)], args)


def cmd_o11y_traces(args: argparse.Namespace) -> None:
    """Find sample APM traces for a service (via the Observability MCP gateway)."""
    mcp = get_o11y_mcp_client(get_single_profile(args))
    env = args.env
    if not env:
        envs = apm_environments(mcp)
        if len(envs) != 1:
            print(
                "Pass --env. Available environments: " + (", ".join(envs[:30]) or "(none)"),
                file=sys.stderr,
            )
            sys.exit(1)
        env = envs[0]

    tags: dict[str, list[str]] = {}
    for item in args.tag or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"--tag expects key=value, got {item!r}", file=sys.stderr)
            sys.exit(1)
        tags.setdefault(key, []).append(value)

    kind = {"any": "req", "error": "err", "root-cause": "rc_err"}[args.type]
    rows, status = exemplar_traces(
        mcp, args.service, env, args.earliest, args.latest, kind, args.min_ms, args.max_ms, tags or None
    )
    if status.get("is_partial") or status.get("warning"):
        print(
            "Warning: " + (status.get("warning") or "partial results")
            + " Try a shorter --earliest range or add --tag / --min-ms filters.",
            file=sys.stderr,
        )
    if not rows:
        print("No matching traces.", file=sys.stderr)
        return
    _output_o11y(rows[: args.limit], args)


def cmd_o11y_trace(args: argparse.Namespace) -> None:
    """Download the spans of an APM trace by ID."""
    client = get_o11y_client(get_single_profile(args))
    _output_o11y(client.trace(args.trace_id), args)


def cmd_mcp_set(args: argparse.Namespace) -> None:
    """Save an MCP server URL and token into a profile's TOML block."""
    name = resolve_profile_name(get_single_profile(args))

    url = args.url or input("MCP server URL: ").strip()
    if not url:
        print("MCP server URL is required.", file=sys.stderr)
        sys.exit(1)

    token = args.token or input("MCP bearer token: ").strip()
    if not token:
        print("MCP token is required.", file=sys.stderr)
        sys.exit(1)

    set_mcp_config(name, url, token)
    print(f"Saved mcp_url/mcp_token for profile '{name}'.")


def cmd_mcp_tools(args: argparse.Namespace) -> None:
    """List tools exposed by the profile's MCP server."""
    client = get_mcp_client(get_single_profile(args))
    tools = client.list_tools()
    if not tools:
        print("No tools found.")
        return
    for tool in tools:
        print(f"{tool['name']}: {tool.get('description', '')}")
        if args.schema:
            print(json.dumps(tool.get("inputSchema", {}), indent=2))


def _parse_tool_arg_value(value: str):
    """Parse a tool argument value, falling back to a raw string."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def cmd_mcp_call(args: argparse.Namespace) -> None:
    """Call an MCP tool by name with key=value arguments."""
    client = get_mcp_client(get_single_profile(args))

    arguments: dict = {}
    for pair in args.args:
        if "=" not in pair:
            print(f"Error: argument {pair!r} is not in key=value form", file=sys.stderr)
            sys.exit(1)
        key, value = pair.split("=", 1)
        arguments[key] = _parse_tool_arg_value(value)

    result = client.call_tool(args.tool, arguments)

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return

    # Default: print text content blocks as-is, fall back to full result.
    content = result.get("content")
    if content:
        for block in content:
            if block.get("type") == "text":
                print(block["text"])
            else:
                print(json.dumps(block, indent=2))
    else:
        print(json.dumps(result, indent=2))


def main() -> None:
    profile_parent = argparse.ArgumentParser(add_help=False)
    profile_parent.add_argument(
        "-p",
        "--profile",
        action="append",
        dest="profiles",
        metavar="PROFILE",
        # SUPPRESS so an absent -p on the subparser doesn't clobber a value
        # given before the subcommand (argparse subparser default behavior).
        default=argparse.SUPPRESS,
        help="Splunk profile to use (repeatable for parallel search)",
    )

    parser = argparse.ArgumentParser(
        prog="splank",
        description="CLI tool for querying Splunk logs",
        parents=[profile_parent],
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # --mcp / --rest: choose the search backend (default: the profile's `mode`).
    backend_parent = argparse.ArgumentParser(add_help=False)
    backend_group = backend_parent.add_mutually_exclusive_group()
    backend_group.add_argument(
        "--mcp",
        dest="mcp",
        action="store_true",
        default=None,
        help="Run the search through the profile's MCP server instead of the REST API",
    )
    backend_group.add_argument(
        "--rest",
        dest="mcp",
        action="store_false",
        help="Force the REST API even if the profile has mode = \"mcp\"",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize credentials")
    init_parser.add_argument(
        "name", nargs="?", help="Profile name to add (e.g. qa) - runs a short wizard instead of opening an editor"
    )
    init_parser.add_argument(
        "host", nargs="?", help="Splunk host for the profile (bare stack name ok, e.g. mystack)"
    )
    init_parser.add_argument(
        "--mcp",
        action="store_true",
        help="Also set up the MCP server for this profile (derives the URL from host, prompts for its token)",
    )
    init_parser.set_defaults(func=cmd_init)

    # search command
    search_options_parent = argparse.ArgumentParser(add_help=False)
    search_options_parent.add_argument(
        "--earliest", "-e", default="-24h", help="Earliest time (default: -24h)"
    )
    search_options_parent.add_argument(
        "--latest", "-l", default="now", help="Latest time (default: now)"
    )
    search_options_parent.add_argument(
        "--max-results", "-m", type=int, default=100, help="Max results (default: 100)"
    )
    search_options_parent.add_argument(
        "--format",
        "-f",
        choices=["json", "csv", "table", "toon"],
        default="toon",
        help="Output format (default: toon)",
    )
    search_options_parent.add_argument(
        "--output", "-o", help="Output file (default: stdout)"
    )
    search_options_parent.add_argument(
        "--internal",
        action="store_true",
        help="Include internal Splunk fields (_bkt, _cd, etc.)",
    )
    search_options_parent.add_argument(
        "--width",
        "-w",
        type=int,
        default=500,
        metavar="N",
        help="Truncate field values to N chars (default: 500, 0=no limit)",
    )
    search_options_parent.add_argument(
        "--zoom",
        "-z",
        action="store_true",
        help="Parse JSON from _raw and output as toon (ignores other fields)",
    )
    search_options_parent.add_argument(
        "--no-sanitize",
        action="store_true",
        help="Disable masking of sensitive data (email addresses) in output",
    )

    search_parser = subparsers.add_parser(
        "search", help="Execute SPL query", parents=[profile_parent, backend_parent, search_options_parent]
    )
    search_parser.add_argument("query", help="SPL query to execute")
    search_parser.set_defaults(func=cmd_search)

    # ask command: free-form question -> SAIA-generated SPL -> search
    ask_parser = subparsers.add_parser(
        "ask",
        help="Ask a free-form question; generates SPL via MCP/SAIA and runs it",
        parents=[profile_parent, backend_parent, search_options_parent],
    )
    ask_parser.add_argument("question", help="Free-form natural-language question")
    ask_parser.add_argument(
        "--dry-run", action="store_true", help="Only print the generated SPL; don't run it"
    )
    ask_parser.set_defaults(func=cmd_ask)

    # discover command
    discover_parser = subparsers.add_parser(
        "discover", help="Discover available indexes", parents=[profile_parent, backend_parent]
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
    jobs_parser = subparsers.add_parser("jobs", help="List search jobs", parents=[profile_parent])
    jobs_parser.add_argument(
        "--mine", action="store_true", help="Show only my jobs"
    )
    jobs_parser.set_defaults(func=cmd_jobs)

    # clear command
    clear_parser = subparsers.add_parser("clear", help="Clear my search jobs", parents=[profile_parent])
    clear_parser.set_defaults(func=cmd_clear)

    # mcp command group
    mcp_parser = subparsers.add_parser("mcp", help="Interact with a Splunk MCP server")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", help="MCP subcommands")

    mcp_set_parser = mcp_subparsers.add_parser(
        "set", help="Save MCP server URL and token for a profile", parents=[profile_parent]
    )
    mcp_set_parser.add_argument("url", nargs="?", help="MCP server URL")
    mcp_set_parser.add_argument("token", nargs="?", help="Bearer token for the MCP server")
    mcp_set_parser.set_defaults(func=cmd_mcp_set)

    mcp_tools_parser = mcp_subparsers.add_parser(
        "tools", help="List tools exposed by the MCP server", parents=[profile_parent]
    )
    mcp_tools_parser.add_argument(
        "--schema", action="store_true", help="Also print each tool's input schema"
    )
    mcp_tools_parser.set_defaults(func=cmd_mcp_tools)

    mcp_call_parser = mcp_subparsers.add_parser(
        "call", help="Call an MCP tool", parents=[profile_parent]
    )
    mcp_call_parser.add_argument("tool", help="Tool name")
    mcp_call_parser.add_argument(
        "args", nargs="*", help="Tool arguments as key=value pairs (values parsed as JSON when possible)"
    )
    mcp_call_parser.add_argument(
        "--format", "-f", choices=["json", "text"], default="text", help="Output format (default: text)"
    )
    mcp_call_parser.set_defaults(func=cmd_mcp_call)

    def cmd_mcp(args: argparse.Namespace) -> None:
        if not args.mcp_command:
            mcp_parser.print_help()
            sys.exit(1)

    mcp_parser.set_defaults(func=cmd_mcp)

    # oy command group (Splunk Observability Cloud / SignalFx)
    o11y_parser = subparsers.add_parser(
        "oy", help="Query Splunk Observability Cloud (SignalFx)"
    )
    o11y_subparsers = o11y_parser.add_subparsers(dest="o11y_command", help="Observability subcommands")

    o11y_set_parser = o11y_subparsers.add_parser(
        "set", help="Save Observability realm and API token for a profile", parents=[profile_parent]
    )
    o11y_set_parser.add_argument("realm", nargs="?", help="Realm, e.g. eu0 or us1")
    o11y_set_parser.add_argument("token", nargs="?", help="Observability API token")
    o11y_set_parser.set_defaults(func=cmd_o11y_set)

    o11y_options_parent = argparse.ArgumentParser(add_help=False)
    o11y_options_parent.add_argument(
        "--limit", "-n", type=int, default=100, help="Max results (default: 100)"
    )
    o11y_options_parent.add_argument(
        "--format", "-f", choices=["json", "csv", "toon"], default="toon",
        help="Output format (default: toon)",
    )
    o11y_options_parent.add_argument("--output", "-o", help="Output file (default: stdout)")

    for cmd, help_text in (
        ("metrics", "Search metric names"),
        ("mts", "Search metric time series metadata"),
    ):
        sp = o11y_subparsers.add_parser(
            cmd, help=help_text, parents=[profile_parent, o11y_options_parent]
        )
        sp.add_argument(
            "query", nargs="?", help="Query, e.g. 'sf_metric:cpu*' or 'host:web-1' (default: all)"
        )
        sp.set_defaults(func=cmd_o11y_query)

    o11y_det_parser = o11y_subparsers.add_parser(
        "detectors", help="Search detectors", parents=[profile_parent, o11y_options_parent]
    )
    o11y_det_parser.add_argument("name", nargs="?", help="Detector name (substring match)")
    o11y_det_parser.set_defaults(func=cmd_o11y_query)

    o11y_inc_parser = o11y_subparsers.add_parser(
        "incidents", help="List incidents", parents=[profile_parent, o11y_options_parent]
    )
    o11y_inc_parser.add_argument(
        "--resolved", action="store_true", help="Include resolved incidents"
    )
    o11y_inc_parser.add_argument(
        "--detector", help="Only incidents whose detector name contains this text (case-insensitive)"
    )
    o11y_inc_parser.add_argument(
        "--since", help="Only incidents last updated at/after this time (e.g. -7d, 2026-10-24)"
    )
    o11y_inc_parser.add_argument(
        "--until", help="Only incidents last updated at/before this time"
    )
    o11y_inc_parser.set_defaults(func=cmd_o11y_query)

    o11y_sf_parser = o11y_subparsers.add_parser(
        "query",
        help="Run a SignalFlow program and print its datapoints",
        parents=[profile_parent, o11y_options_parent],
    )
    o11y_sf_parser.add_argument(
        "program", help="SignalFlow program, e.g. \"data('cpu.utilization').mean(by=['aws_region']).publish()\""
    )
    o11y_sf_parser.add_argument("--earliest", "-e", default="-1h", help="Start (default: -1h)")
    o11y_sf_parser.add_argument("--latest", "-l", default="now", help="End (default: now)")
    o11y_sf_parser.add_argument(
        "--resolution", "-r", help="Minimum data resolution, e.g. 1m, 5m (default: chosen by server)"
    )
    o11y_sf_parser.set_defaults(func=cmd_o11y_signalflow)

    o11y_cmp_parser = o11y_subparsers.add_parser(
        "compare",
        help="Compare a metric between a before and an after window, per group (optionally vs a control)",
        parents=[profile_parent, o11y_options_parent],
    )
    o11y_cmp_parser.add_argument(
        "program", help="SignalFlow program ending in .publish(); group dimensions must survive (e.g. mean(by=['host']))"
    )
    o11y_cmp_parser.add_argument(
        "--before", nargs=2, required=True, metavar=("START", "END"), help="Before window (a date-only END includes that day)"
    )
    o11y_cmp_parser.add_argument(
        "--after", nargs=2, required=True, metavar=("START", "END"), help="After window"
    )
    o11y_cmp_parser.add_argument(
        "--group-by", "-g", help="Dimension(s) to compare per, comma-separated (default: all dimensions of the published series)"
    )
    o11y_cmp_parser.add_argument(
        "--focus", help="Comma-separated group values that are the subject (e.g. rollout clusters); the rest are labeled control"
    )
    o11y_cmp_parser.add_argument(
        "--stat", choices=["median", "mean", "max", "min"], default="median", help="Statistic per window (default: median)"
    )
    o11y_cmp_parser.add_argument(
        "--resolution", "-r", default="1d", help="Datapoint resolution (default: 1d)"
    )
    o11y_cmp_parser.add_argument(
        "--min-points", type=int, default=5, help="Min datapoints per window per group (default: 5)"
    )
    o11y_cmp_parser.add_argument(
        "--include-weekends", action="store_true", help="Keep Saturday/Sunday datapoints (default: excluded, UTC)"
    )
    o11y_cmp_parser.set_defaults(func=cmd_o11y_compare)

    o11y_svc_parser = o11y_subparsers.add_parser(
        "services", help="List APM services", parents=[profile_parent, o11y_options_parent]
    )
    o11y_svc_parser.add_argument("patterns", nargs="*", help="Glob patterns to filter names (e.g. 'onp*')")
    o11y_svc_parser.add_argument("--earliest", "-e", default="-1h", help="Start (default: -1h)")
    o11y_svc_parser.add_argument("--latest", "-l", default="now", help="End (default: now)")
    o11y_svc_parser.add_argument("--edges", action="store_true", help="List dependency edges instead of services")
    o11y_svc_parser.add_argument(
        "--type", help="Only nodes of this type (service, database, pubsub)"
    )
    o11y_svc_parser.set_defaults(func=cmd_o11y_services)

    o11y_tools_parser = o11y_subparsers.add_parser(
        "tools",
        help="List the tools of the Observability MCP gateway, or show one tool's fields",
        parents=[profile_parent, o11y_options_parent],
    )
    o11y_tools_parser.add_argument(
        "name", nargs="?", help="Tool name to describe, or text to filter the list by"
    )
    o11y_tools_parser.add_argument(
        "--toolsets", help="Comma-separated extra toolsets to expose (e.g. apm,detectors; see 'oy tool list_mcp_toolsets')"
    )
    o11y_tools_parser.set_defaults(func=cmd_o11y_tools)

    o11y_tool_parser = o11y_subparsers.add_parser(
        "tool",
        help="Call any tool of the Observability MCP gateway directly",
        parents=[profile_parent, o11y_options_parent],
    )
    o11y_tool_parser.add_argument("tool", help="Tool name (the 'o11y_' prefix is optional)")
    o11y_tool_parser.add_argument(
        "args", nargs="*", help="Arguments as key=value; dotted keys nest (time_range.start=-1h); values parsed as JSON when possible"
    )
    o11y_tool_parser.add_argument("--json", dest="json_args", help="Arguments as a JSON object (key=value pairs override it)")
    o11y_tool_parser.add_argument("--toolsets", help="Comma-separated extra toolsets to expose for this call")
    o11y_tool_parser.add_argument("--raw", action="store_true", help="Print the tool's text result unchanged")
    o11y_tool_parser.set_defaults(func=cmd_o11y_tool)

    o11y_envs_parser = o11y_subparsers.add_parser(
        "envs", help="List APM environments", parents=[profile_parent, o11y_options_parent]
    )
    o11y_envs_parser.set_defaults(func=cmd_o11y_envs)

    o11y_traces_parser = o11y_subparsers.add_parser(
        "traces",
        help="Find sample APM traces for a service (errors, slow, tagged)",
        parents=[profile_parent, o11y_options_parent],
    )
    o11y_traces_parser.add_argument("service", help="APM service name")
    o11y_traces_parser.add_argument(
        "--env", help="APM environment (default: the only one, if there is just one; see 'oy envs')"
    )
    o11y_traces_parser.add_argument(
        "--type",
        choices=["any", "error", "root-cause"],
        default="any",
        help="any trace, traces with errors, or traces where this service is the root cause (default: any)",
    )
    o11y_traces_parser.add_argument("--min-ms", type=float, help="Only traces at least this long (ms)")
    o11y_traces_parser.add_argument("--max-ms", type=float, help="Only traces at most this long (ms)")
    o11y_traces_parser.add_argument(
        "--tag", action="append", metavar="KEY=VALUE", help="Span tag filter (repeatable; same key = any of)"
    )
    o11y_traces_parser.add_argument("--earliest", "-e", default="-1h", help="Start (default: -1h)")
    o11y_traces_parser.add_argument("--latest", "-l", default="now", help="End (default: now)")
    o11y_traces_parser.set_defaults(func=cmd_o11y_traces)

    o11y_trace_parser = o11y_subparsers.add_parser(
        "trace", help="Download an APM trace's spans by trace ID", parents=[profile_parent, o11y_options_parent]
    )
    o11y_trace_parser.add_argument("trace_id", help="Trace ID")
    o11y_trace_parser.set_defaults(func=cmd_o11y_trace)

    def cmd_o11y(args: argparse.Namespace) -> None:
        if not args.o11y_command:
            o11y_parser.print_help()
            sys.exit(1)

    o11y_parser.set_defaults(func=cmd_o11y)

    args = parser.parse_args()

    # SUPPRESS leaves profiles unset when -p is never given; normalize it.
    if not hasattr(args, "profiles"):
        args.profiles = None

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
