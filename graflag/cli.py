"""Command Line Interface for GraFlag."""

import sys
import argparse
import logging
import traceback

from .core import GraFlag, GraFlagError

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="GraFlag - Graph Anomaly Detection Benchmarking Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  graflag.py setup                                    # Setup cluster
  graflag.py benchmark --method Dummy --dataset Cora # Run benchmark
  graflag.py benchmark -m DeepWalk -d CiteSeer --build # Build and run
  graflag.py status                                   # Show cluster status
  graflag.py list methods                            # List available methods
  graflag.py logs --experiment exp__dummy__cora__20250924_161245 # Show logs
  graflag.py logs -e exp__dummy__cora__20250924_161245 -f # Follow logs
  graflag.py logs -e exp__dummy__cora__20250924_161245 --tee ./logs/exp.log # Save to file
  graflag.py copy --source ./data --dest datasets -r # Copy local data to remote
  graflag.py copy -s file1.txt file2.txt --dest data # Copy multiple files
  graflag.py copy -s ./dir1 ./dir2 ./file.txt --dest backup -r # Copy mixed items
        """,
    )

    parser.add_argument(
        "command",
        choices=["setup", "benchmark", "status", "list", "copy", "logs"],
        help="Command to execute",
    )

    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["methods", "datasets", "experiments"],
        help="Subcommand for list command",
    )

    parser.add_argument("--method", "-m", help="Method name for benchmark")

    parser.add_argument("--dataset", "-d", help="Dataset name for benchmark")

    parser.add_argument(
        "--tag", "-t", default="latest", help="Docker image tag (default: latest)"
    )

    parser.add_argument(
        "--build", "-b", action="store_true", help="Build image before running"
    )

    parser.add_argument(
        "--config", "-c", default=".env", help="Configuration file (default: .env)"
    )

    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    
    parser.add_argument(
        "--source", "-s", nargs='+', help="Source path(s) for copy command (can specify multiple files/directories)"
    )
    
    parser.add_argument(
        "--dest", help="Destination path for copy command (relative to shared dir)"
    )
    
    parser.add_argument(
        "--recursive", "-r", action="store_true", help="Copy directories recursively"
    )
    
    parser.add_argument(
        "--experiment", "-e", help="Experiment name for logs command"
    )
    
    parser.add_argument(
        "--follow", "-f", action="store_true", help="Follow log output (like tail -f)"
    )
    
    parser.add_argument(
        "--tee", help="Save logs to file while displaying on terminal (like tee command)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        gf = GraFlag(config_file=args.config)

        if args.command == "setup":
            gf.setup()

        elif args.command == "benchmark":
            if not args.method or not args.dataset:
                parser.error("benchmark requires --method and --dataset")
            gf.benchmark(args.method, args.dataset, args.tag, args.build)

        elif args.command == "status":
            gf.status()

        elif args.command == "list":
            if args.subcommand == "methods":
                gf.list_methods()
            elif args.subcommand == "datasets":
                gf.list_datasets()
            elif args.subcommand == "experiments":
                gf.list_experiments()
            else:
                parser.error(
                    "list command requires subcommand: methods, datasets, or experiments"
                )
        
        elif args.command == "copy":
            if not args.source or not args.dest:
                parser.error("copy command requires --source and --dest")
            gf.copy_to_remote(args.source, args.dest, args.recursive)
        
        elif args.command == "logs":
            if not args.experiment:
                parser.error("logs command requires --experiment")
            gf.logs(args.experiment, args.follow, args.tee)

    except GraFlagError as e:
        logger.error(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("👋 Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()