"""Command Line Interface for GraFlag."""

import os
import sys
import json
import argparse
import logging
import traceback
from pathlib import Path

from .core import GraFlag, GraFlagError
from .config import get_config_path, init_config, GraflagConfig

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="GraFlag - Graph Anomaly Detection Benchmarking Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  graflag setup                                    # Setup cluster
  graflag setup --reconfigure                      # Re-run config wizard
  graflag run --method Dummy --dataset Cora        # Run experiment with GPU
  graflag run -m taddy -d uci --build --params MAX_EPOCH=100 LEARNING_RATE=0.001
  graflag run -m DeepWalk -d CiteSeer --no-gpu     # Run without GPU
  graflag run --from-config ./experiments/exp__method__dataset__timestamp/service_config.json
  graflag status                                   # Show cluster status
  graflag list methods                             # List available methods
  graflag list services                            # List running services
  graflag logs -e exp__dummy__cora__20250924_161245 # Show logs
  graflag logs -e exp__dummy__cora__20250924_161245 -f # Follow logs
  graflag stop -e exp__dummy__cora__20250924_161245 # Stop experiment
  graflag evaluate -e exp__generaldyg__btc_alpha__20251211_120000 # Evaluate
  graflag verify -e exp__generaldyg__btc_alpha__20251211_120000   # Check the result holds up
  graflag cleanup                                  # Remove finished services
  graflag cleanup --dry-run                        # Show what would be removed
  graflag clear                                    # Report orphaned storage and images
  graflag clear --apply                            # Remove it
  graflag clear --apply --gc                       # Also reclaim registry blobs
  graflag clear --images                           # Only images and registry repos
  graflag run -m taddy -d uci --keep-service       # Keep the service after the run
  graflag run -m taddy -d uci --build --force-rm   # A failed build leaves no container
  graflag copy -s ./data --dest datasets -r        # Copy to remote (-d is --dataset)
  graflag copy --from-remote -s experiments/exp --dest ./local # Copy from remote
  graflag sync                                     # Sync current method dir
  graflag sync --lib --path ./my-lib/              # Sync a shared library
  graflag gui                                      # Start web dashboard
  graflag gui --port 8080 --debug                  # GUI on custom port
  graflag mcp                                      # MCP server for AI agents (stdio)
  graflag devcluster --hosts hosts.yml             # Deploy virtual cluster
  graflag devcluster --hosts hosts.yml --pubkey ~/.ssh/id_rsa.pub
  graflag devcluster --down                        # Stop and remove cluster
        """,
    )

    parser.add_argument(
        "command",
        choices=["setup", "run", "status", "list", "copy", "logs", "stop", "evaluate",
                 "verify", "cleanup", "clear", "sync", "gui", "mcp", "devcluster"],
        help="Command to execute",
    )
    parser.add_argument(
        "subcommand", nargs="?",
        choices=["methods", "datasets", "experiments", "services"],
        help="Subcommand for list command",
    )
    parser.add_argument("--method", "-m", help="Method name for run")
    parser.add_argument("--dataset", "-d", help="Dataset name for run")
    parser.add_argument("--tag", "-t", default=None,
                        help="Docker image tag (default: latest, or the recorded one with --from-config)")
    parser.add_argument("--build", "-b", action="store_true", help="Build image before running")
    parser.add_argument(
        "--config", "-c", default=None,
        help="Configuration file (default: ./.env if it is a GraFlag config, "
             "else ~/.config/graflag/config.env)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--source", "-s", nargs='+',
        help="Source path(s) for copy command. rsync semantics: 'dir' copies "
             "dir itself into --dest, 'dir/' copies its contents there",
    )
    parser.add_argument(
        "--dest",
        help="Destination path for copy command (no short flag: -d is --dataset)",
    )
    parser.add_argument(
        "--recursive", "-r", action="store_true",
        help="Accepted for compatibility; directories are always copied recursively",
    )
    parser.add_argument("--from-remote", action="store_true", help="Copy from remote to local")
    parser.add_argument("--experiment", "-e", help="Experiment name for logs/stop commands")
    parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    parser.add_argument("--rm", action="store_true", help="Also delete experiment directory on stop")
    parser.add_argument(
        "--keep-service", action="store_true",
        help="Leave the finished Swarm service in place after run (default: remove it)",
    )
    parser.add_argument(
        "--force-rm", action="store_true",
        help="With --build: remove the build's intermediate containers even if "
             "the build fails (docker build --force-rm). Without it a failed "
             "build leaves its last container, and every layer under it, on "
             "the manager",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="For cleanup: report what would be removed without removing it",
    )
    # `clear` deletes storage rather than services, so its default is the
    # opposite of cleanup's: it reports unless told to act.
    parser.add_argument(
        "--apply", action="store_true",
        help="For clear: actually remove what is reported (default: report only)",
    )
    parser.add_argument(
        "--share", action="store_true",
        help="For clear: limit the sweep to the share (experiments, datasets, strays)",
    )
    parser.add_argument(
        "--images", action="store_true",
        help="For clear: limit the sweep to images and registry repositories",
    )
    parser.add_argument(
        "--gc", action="store_true",
        help="For clear: also garbage-collect registry blobs, which is what "
             "reclaims the disk. Stops the registry for the duration",
    )
    parser.add_argument("--tee", help="Save logs to file while displaying")
    parser.add_argument("--json", action="store_true",
                        help="For verify: also print the probe summary the checks read")
    # default=None on both, so _parse_run_args can tell a flag from its absence:
    # a replay keeps the recorded GPU choice unless the command line says otherwise.
    parser.add_argument("--gpu", "-g", action="store_true", default=None,
                        help="Reserve a GPU (the default; with --from-config, the recorded choice)")
    parser.add_argument("--no-gpu", action="store_false", dest="gpu", default=None,
                        help="Run without a GPU")
    parser.add_argument("--params", "-p", nargs='+', metavar="KEY=VALUE", help="Method parameters")
    parser.add_argument(
        "--from-config", metavar="CONFIG_FILE",
        help="Load method/dataset/params from a config file",
    )
    parser.add_argument("--lib", action="store_true", help="Sync as a shared library")
    parser.add_argument("--path", default=None, help="Local path for sync command")
    # GUI args
    parser.add_argument("--host", default="0.0.0.0", help="GUI server host (default: 0.0.0.0)")
    parser.add_argument("--port", default=5000, type=int, help="GUI server port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable GUI debug mode")
    # Devcluster args
    parser.add_argument("--hosts", default=None, help="Path to hosts.yml for devcluster")
    parser.add_argument("--pubkey", default=None, help="Path to SSH public key for devcluster")
    parser.add_argument("--down", action="store_true", help="Stop and remove devcluster")
    parser.add_argument("--reconfigure", action="store_true", help="Re-run configuration wizard for setup")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # GUI: start web dashboard (doesn't need GraFlag instance directly)
        if args.command == "gui":
            from .gui.server import serve
            serve(args.config, args.host, args.port, args.debug)
            return

        # MCP: serve GraFlag's tools to an AI agent over stdio. Before anything
        # else touches stdout, which from here on belongs to the protocol.
        if args.command == "mcp":
            try:
                from .mcp_server import serve as serve_mcp
            except ImportError as exc:
                logger.error(
                    "[ERROR] graflag mcp needs the MCP SDK, which needs Python "
                    f"3.10+: pip install 'graflag[mcp]' ({exc})")
                sys.exit(1)
            serve_mcp(args.config)
            return

        # Devcluster: deploy or tear down virtual cluster
        if args.command == "devcluster":
            if not args.hosts and not args.down:
                parser.error("devcluster requires --hosts <path-to-hosts.yml> or --down")
            from .devcluster.cli import main as devcluster_main
            devcluster_main(args.hosts, args.pubkey, args.down)
            return

        # Setup: create or update config interactively
        if args.command == "setup":
            config_path = get_config_path(args.config)
            needs_wizard = args.reconfigure or not config_path.exists()
            if not needs_wizard:
                # An existing file that cannot be used is not a reason to skip
                # the wizard; doing so left `graflag setup` telling the user to
                # run `graflag setup`.
                try:
                    GraflagConfig(args.config)
                except ValueError as exc:
                    logger.warning(f"[WARN] {config_path}: {exc}")
                    needs_wizard = True
            if needs_wizard:
                init_config(config_path if args.config else None)

        gf = GraFlag(config_file=args.config)

        if args.command == "setup":
            gf.setup()
            # Show status after setup
            _print_status(gf.status())

        elif args.command == "run":
            method, dataset, tag, gpu, method_params = _parse_run_args(args, parser)
            gf.run(method, dataset, tag, args.build, gpu, method_params,
                   keep_service=args.keep_service, force_rm=args.force_rm)

        elif args.command == "status":
            _print_status(gf.status())

        elif args.command == "list":
            if args.subcommand == "methods":
                _print_methods(gf.list_methods())
            elif args.subcommand == "datasets":
                _print_datasets(gf.list_datasets())
            elif args.subcommand == "experiments":
                _print_experiments(gf.list_experiments())
            elif args.subcommand == "services":
                _print_services(gf.list_services())
            else:
                parser.error("list command requires subcommand: methods, datasets, experiments, or services")

        elif args.command == "copy":
            if not args.source or not args.dest:
                # -d is --dataset, not --dest; the docs used to show `-d` here
                # and it silently bound to the wrong option.
                parser.error(
                    "copy command requires --source/-s and --dest "
                    "(note: -d is --dataset, so --dest must be spelled out)"
                )
            gf.copy_files(args.source, args.dest, args.recursive, args.from_remote)

        elif args.command == "logs":
            if not args.experiment:
                parser.error("logs command requires --experiment")
            if args.follow:
                gf.follow_logs(args.experiment, args.tee)
            else:
                gf.show_logs(args.experiment, args.tee)

        elif args.command == "stop":
            if not args.experiment:
                parser.error("stop command requires --experiment")
            gf.stop(args.experiment, remove=args.rm)

        elif args.command == "evaluate":
            if not args.experiment:
                parser.error("evaluate command requires --experiment")
            gf.evaluate(args.experiment)

        elif args.command == "verify":
            if not args.experiment:
                parser.error("verify command requires --experiment")
            report = gf.verify(args.experiment)
            if args.json:
                print(json.dumps(report.probe, indent=2, sort_keys=True))
            _print_verification(report)
            if report.failed:
                sys.exit(1)

        elif args.command == "clear":
            # Neither scope flag means both; naming one narrows the sweep.
            both = not (args.share or args.images)
            _print_clear(gf.clear(
                apply=args.apply,
                share=both or args.share,
                images=both or args.images,
                collect=args.gc,
            ))

        elif args.command == "cleanup":
            _print_cleanup(
                gf.cleanup_services(experiment=args.experiment, dry_run=args.dry_run),
                dry_run=args.dry_run,
            )

        elif args.command == "sync":
            local_path = args.path or os.getcwd()
            gf.sync(local_path, is_lib=args.lib)

    except GraFlagError as e:
        logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)


# ============================================================================
# Output Formatting
# ============================================================================

def _print_verification(report):
    """Print `graflag verify` findings, one per line, then the tally."""
    from .verify import format_report
    for line in format_report(report):
        print(line)


def _parse_run_args(args, parser):
    """Parse run arguments from CLI args.

    Returns ``(method, dataset, tag, gpu, method_params)``. With --from-config
    every one of them comes from the recorded run, and whatever the command
    line names overrides it.
    """
    # Without --build nothing is built, so --force-rm would be accepted and do
    # nothing, and a run that was meant to clean up after itself would not.
    if args.force_rm and not args.build:
        parser.error("--force-rm applies to the image build; use it with --build")

    method = args.method
    dataset = args.dataset
    tag, gpu = args.tag, args.gpu        # None when the command line leaves them out
    method_params = {}

    if args.from_config:
        config_path = Path(args.from_config)
        if not config_path.exists():
            parser.error(f"Config file not found: {args.from_config}")

        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            parser.error(f"Invalid JSON in config file: {e}")

        if not method:
            method = config.get('method_name')
        if not dataset:
            dataset = config.get('dataset')
        # The recorded image tag and GPU choice, unless the command line names
        # its own. Reading only method, dataset and parameters replayed a
        # --no-gpu run on a GPU, and a run on a tagged image on `latest`.
        if tag is None:
            tag = config.get('tag')
        if gpu is None:
            gpu = config.get('gpu_required')

        env_contents = config.get('env_contents', {})
        for key, value in env_contents.items():
            if key.startswith('_'):
                method_params[key[1:]] = str(value)
        # env_contents holds the .env's defaults as well as the user's
        # overrides, so a recorded `_GPU` is usually just the method's default
        # index. Passed on as a parameter it counts as explicit, and
        # _build_service_env then leaves it alone instead of switching a
        # replay without a GPU to -1. An explicit --params GPU=... below still
        # wins. (A recorded override of `_GPU` on a GPU-less run is dropped
        # too; the record cannot tell it from the default.)
        if gpu is False:
            method_params.pop('GPU', None)

    if args.params:
        for param in args.params:
            if '=' not in param:
                parser.error(f"Invalid parameter format: {param}. Use KEY=VALUE.")
            key, value = param.split('=', 1)
            method_params[key] = value

    if not method or not dataset:
        parser.error("run requires --method and --dataset (or --from-config)")

    return method, dataset, tag or "latest", True if gpu is None else gpu, method_params


def _print_status(cluster_info):
    """Format and print cluster status."""
    if cluster_info.error:
        print(f"[ERROR] {cluster_info.error}")
        return

    print(f"\n[INFO] Manager: {cluster_info.manager_ip}")
    print(f"[INFO] Swarm: {'active' if cluster_info.swarm_initialized else 'inactive'}")

    if cluster_info.worker_nodes:
        print("\n[INFO] Nodes:")
        for node in cluster_info.worker_nodes:
            role = "manager" if node.get('is_manager') else "worker"
            print(f"  - {node['hostname']}: {node['status']} ({role}, {node['availability']})")

    if cluster_info.services:
        print(f"\n[INFO] Running Services:")
        print(f"  {'NAME':<50} {'REPLICAS':<15} {'IMAGE':<30}")
        print("  " + "-" * 95)
        for svc in cluster_info.services:
            name = svc['name'][:49]
            replicas = svc.get('replicas', '')[:14]
            image = svc.get('image', '')[:29]
            print(f"  {name:<50} {replicas:<15} {image:<30}")
    else:
        print("\n[INFO] Running Services: None")

    print(f"\n[INFO] Shared Directory: {cluster_info.shared_dir}")
    if cluster_info.shared_contents:
        print("  Contents:")
        for item in cluster_info.shared_contents:
            print(f"    - {item}")


def _print_methods(methods):
    """Format and print method list.

    The provenance sits next to the name rather than in a detail view nobody
    opens: whether a score came from the authors' implementation or from a
    reimplementation in this repository decides what a comparison between two
    rows means, and this listing is where the comparison starts.
    """
    if not methods:
        print("[INFO] No methods found")
        return

    # A method whose .env predates INTEGRATION reads as "unstated" -- an absent
    # declaration is not evidence for either kind, and guessing one would put
    # the mislabelling this column exists to prevent back into the output.
    integrations = {m.name: (m.integration or "unstated") for m in methods}
    name_w = max(len(m.name) for m in methods)
    kind_w = max(len(v) for v in integrations.values())

    print("[INFO] Available Methods:")
    for m in methods:
        print(f"  - {m.name:<{name_w}}  {integrations[m.name]:<{kind_w}}  "
              f"(Supports: {m.supported_data})")


def _print_datasets(datasets):
    """Format and print dataset list."""
    if not datasets:
        print("[INFO] No datasets found")
        return

    print("[INFO] Available Datasets:")
    for d in datasets:
        size_str = f" ({d.size_mb:.1f} MB, {d.file_count} files)" if d.size_mb > 0 else ""
        print(f"  - {d.name}{size_str}")


def _print_experiments(experiments):
    """Format and print experiment list."""
    if not experiments:
        print("[INFO] No experiments found")
        return

    print("[INFO] Recent Experiments:")
    for e in experiments:
        tags = f"[{e.status}]"
        if e.has_results:
            tags += " [results]"
        if e.has_evaluation:
            tags += " [eval]"
        print(f"  - {e.name}  {tags}")


def _human_bytes(n: int) -> str:
    """Bytes as the report prints them. 0 renders as "-", not "0 B".

    A registry repository has no measured size until the blobs are collected,
    and printing 0 B beside it would read as "removing this frees nothing"
    rather than "this is not what is measured".
    """
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def _print_clear(report):
    """Format the outcome of `graflag clear`."""
    if not report.items:
        print("[OK] Nothing to clear")
    else:
        verb = "Removed" if report.applied else "Would remove"
        print(f"{verb} {len(report.items)} item(s):\n")
        name_w = max(len(i.name) for i in report.items)
        kind_w = max(len(i.kind) for i in report.items)
        for kind in ("experiment", "dataset", "stray", "image", "registry"):
            group = [i for i in report.items if i.kind == kind]
            for item in group:
                # A dry run has nothing to mark, so the column is only drawn
                # once something could have failed to be removed.
                mark = "" if not report.applied else ("  " if item.removed else " !")
                print(f"  {mark}{item.kind:<{kind_w}}  {item.name:<{name_w}}  "
                      f"{_human_bytes(item.size_bytes):>7}  {item.reason}")

    if report.applied:
        print(f"\n[OK] Freed {_human_bytes(report.freed_bytes)}")
        registry = [i for i in report.items if i.kind == "registry" and i.removed]
        if registry and not report.registry_collected:
            print("[INFO] Registry manifests removed; their layers stay on disk "
                  "until a garbage collection. Re-run with --gc to reclaim them.")
    else:
        print("\n[INFO] Nothing was removed. Re-run with --apply to remove it.")
        if any(i.kind == "registry" for i in report.items):
            print("[INFO] Add --gc to also reclaim the layers behind the "
                  "registry repositories.")

    for error in report.errors:
        print(f"[ERROR] {error}")


def _print_cleanup(results, dry_run: bool = False):
    """Format and print service cleanup outcomes."""
    if not results:
        print("[INFO] No finished services to clean up")
        return

    removed = [r for r in results if r.removed]
    kept = [r for r in results if not r.removed]

    if dry_run:
        would = [r for r in kept if r.reason.startswith("would remove")]
        kept = [r for r in kept if not r.reason.startswith("would remove")]
        for r in sorted(would, key=lambda r: r.experiment):
            print(f"  [DRY]  {r.experiment}")
    else:
        for r in sorted(removed, key=lambda r: r.experiment):
            print(f"  [OK]   {r.experiment}  {r.reason}")

    for r in sorted(kept, key=lambda r: r.experiment):
        print(f"  [SKIP] {r.experiment}  ({r.reason})")

    if dry_run:
        print(f"\n[INFO] Would remove {len(would)} service(s), keep {len(kept)}")
    else:
        print(f"\n[INFO] Removed {len(removed)} service(s), kept {len(kept)}")


def _print_services(services):
    """Format and print running services."""
    if not services:
        print("\n[INFO] Running Services: None")
        return

    print("\n[INFO] Running Services:")
    print(f"  {'NAME':<50} {'REPLICAS':<15} {'IMAGE':<30}")
    print("  " + "-" * 95)
    for svc in services:
        name = svc['name'][:49]
        replicas = str(svc.get('replicas', ''))[:14]
        image = svc.get('image', '')[:29]
        print(f"  {name:<50} {replicas:<15} {image:<30}")


if __name__ == "__main__":
    main()
