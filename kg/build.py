import argparse

from .aspect_extraction import extract_aspects
from .constants import DEFAULT_GAME_COUNT
from .neo4j_loader import load_graph
from .steam_source import collect_live
from .ucsd_download import download_ucsd_files
from .ucsd_source import collect_ucsd

STEPS = ["download", "collect", "aspects", "load"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", choices=["ucsd", "live"], required=True)
    parser.add_argument("--steps", nargs="+", choices=STEPS, default=STEPS)
    parser.add_argument("--games", type=int, default=DEFAULT_GAME_COUNT)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    if "download" in args.steps and args.graph == "ucsd":
        download_ucsd_files()
    if "collect" in args.steps:
        if args.graph == "live":
            collect_live(args.games)
        else:
            collect_ucsd(args.games)
    if "aspects" in args.steps:
        extract_aspects(args.graph, workers=args.workers)
    if "load" in args.steps:
        load_graph(args.graph)


if __name__ == "__main__":
    main()
