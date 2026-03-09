"""Command Line Interface for GraFlag."""

import os
import sys
import json
import argparse
import logging
import traceback
from pathlib import Path

from .core import GraFlag, GraFlagError
from .config import get_config_path, init_config

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
  graflag.py benchmark --method Dummy --dataset Cora # Run benchmark with GPU
  graflag.py benchmark -m taddy -d uci --build --params MAX_EPOCH=100 LEARNING_RATE=0.001
  graflag.py benchmark -m DeepWalk -d CiteSeer --no-gpu # Run without GPU
  graflag.py benchmark -m taddy -d uci -p DATASET=uci ANOMALY_PER=0.1 TRAIN_PER=0.5
  graflag.py benchmark --from-config ./experiments/exp__method__dataset__timestamp/service_config.json
  graflag.py benchmark --from-config ./config.json --params EPOCHS=50  # Override config params
  graflag.py status                                   # Show cluster status and running services
  graflag.py list methods                            # List available methods
  graflag.py list services                           # List running services/experiments
  graflag.py logs --experiment exp__dummy__cora__20250924_161245 # Show logs
  graflag.py logs -e exp__dummy__cora__20250924_161245 -f # Follow logs
  graflag.py logs -e exp__dummy__cora__20250924_161245 --tee ./logs/exp.log # Save to file
  graflag.py stop --experiment exp__dummy__cora__20250924_161245 # Stop running experiment
  graflag.py stop -e exp__dummy__cora__20250924_161245  # Short form
  graflag.py evaluate --experiment exp__generaldyg__btc_alpha__20251211_120000 # Evaluate experiment
  graflag.py evaluate -e exp__generaldyg__btc_alpha__20251211_120000 # Short form
  graflag.py copy --source ./data --dest datasets -r # Copy local data to remote
  graflag.py copy -s file1.txt file2.txt --dest data # Copy multiple files to remote
  graflag.py copy -s ./dir1 ./dir2 ./file.txt --dest backup -r # Copy mixed items to remote
  graflag.py copy --from-remote -s experiments/exp_name -d ./local_results # Copy from remote to local
  graflag.py copy --from-remote -s datasets/cora experiments/exp1 -d ./backup # Copy multiple from remote
  graflag.py sync                                        # Sync current method dir to remote
  graflag.py sync --path ./my-method/                    # Sync specific method dir to remote
  graflag.py sync --lib --path ./my-lib/                 # Sync a shared library to remote
        """,
    )

    parser.add_argument(
        "command",
        choices=["setup", "benchmark", "status", "list", "copy", "logs", "stop", "evaluate", "sync"],
        help="Command to execute",
    )

    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["methods", "datasets", "experiments", "services"],
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
        "--dest", help="Destination path for copy command"
    )
    
    parser.add_argument(
        "--recursive", "-r", action="store_true", help="Copy directories recursively"
    )
    
    parser.add_argument(
        "--from-remote", action="store_true", help="Copy from remote to local (default is local to remote)"
    )
    
    parser.add_argument(
        "--experiment", "-e", help="Experiment name for logs/stop commands"
    )
    
    parser.add_argument(
        "--follow", "-f", action="store_true", help="Follow log output (like tail -f)"
    )

    parser.add_argument(
        "--rm", action="store_true",
        help="For stop command: also delete the experiment directory"
    )
    
    parser.add_argument(
        "--tee", help="Save logs to file while displaying on terminal (like tee command)"
    )
    
    parser.add_argument(
        "--gpu", "-g", action="store_true", default=True, 
        help="Enable GPU support for the benchmark (default: True)"
    )
    
    parser.add_argument(
        "--no-gpu", action="store_false", dest="gpu",
        help="Disable GPU support for the benchmark"
    )
    
    parser.add_argument(
        "--params", "-p", nargs='+', metavar="KEY=VALUE",
        help="Method parameters as key=value pairs (e.g., --params EPOCHS=100 LEARNING_RATE=0.01)"
    )
    
    parser.add_argument(
        "--from-config", metavar="CONFIG_FILE",
        help="Load method, dataset, and parameters from a config file (e.g., service_config.json). "
             "Parameters from --params will override config file values."
    )

    parser.add_argument(
        "--lib", action="store_true",
        help="For sync command: sync as a shared library instead of a method"
    )

    parser.add_argument(
        "--path",
        default=None,
        help="Local path for sync command (default: current directory)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Setup: create config interactively if it doesn't exist
        if args.command == "setup":
            config_path = get_config_path(args.config)
            if not config_path.exists():
                init_config()

        gf = GraFlag(config_file=args.config)

        if args.command == "setup":
            gf.setup()

        elif args.command == "benchmark":
            method = args.method
            dataset = args.dataset
            method_params = {}
            
            # Load from config file if provided
            if args.from_config:
                config_path = Path(args.from_config)
                if not config_path.exists():
                    parser.error(f"Config file not found: {args.from_config}")
                
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                except json.JSONDecodeError as e:
                    parser.error(f"Invalid JSON in config file: {e}")
                
                # Extract method and dataset from config if not provided via CLI
                if not method:
                    method = config.get('method_name')
                if not dataset:
                    dataset = config.get('dataset')
                
                # Extract env_contents parameters (those starting with _)
                env_contents = config.get('env_contents', {})
                for key, value in env_contents.items():
                    if key.startswith('_'):
                        # Remove the leading underscore and use as param
                        param_name = key[1:]  # e.g., _BATCH_SIZE -> BATCH_SIZE
                        method_params[param_name] = str(value)
            
            # Parse --params and override config values
            if args.params:
                for param in args.params:
                    if '=' not in param:
                        parser.error(f"Invalid parameter format: {param}. Use KEY=VALUE format.")
                    key, value = param.split('=', 1)
                    method_params[key] = value
            
            if not method or not dataset:
                parser.error("benchmark requires --method and --dataset (or --from-config)")
            
            gf.benchmark(method, dataset, args.tag, args.build, args.gpu, method_params)

        elif args.command == "status":
            gf.status()

        elif args.command == "list":
            if args.subcommand == "methods":
                gf.list_methods()
            elif args.subcommand == "datasets":
                gf.list_datasets()
            elif args.subcommand == "experiments":
                gf.list_experiments()
            elif args.subcommand == "services":
                gf.list_services()
            else:
                parser.error(
                    "list command requires subcommand: methods, datasets, experiments, or services"
                )
        
        elif args.command == "copy":
            if not args.source or not args.dest:
                parser.error("copy command requires --source and --dest")
            gf.copy_files(args.source, args.dest, args.recursive, args.from_remote)
        
        elif args.command == "logs":
            if not args.experiment:
                parser.error("logs command requires --experiment")
            gf.logs(args.experiment, args.follow, args.tee)
        
        elif args.command == "stop":
            if not args.experiment:
                parser.error("stop command requires --experiment")
            gf.stop(args.experiment, remove=args.rm)
        
        elif args.command == "evaluate":
            if not args.experiment:
                parser.error("evaluate command requires --experiment")
            gf.evaluate(args.experiment)

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


if __name__ == "__main__":
    main()