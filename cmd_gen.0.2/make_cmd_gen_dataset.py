import io
import os
import json
import zipfile
import argparse
import urllib.request
import multiprocessing
from os.path import join as pjoin

import tqdm
import numpy as np
import spacy

import textworld
from textworld.logic import State, Rule, Proposition, Variable

from generic import preproc
from generic import process_facts, serialize_facts, gen_graph_commands
from generic import process_local_obs_facts, process_fully_obs_facts


ZIP_FILENAME = "TextWorld_CoG2019.zip"
GAMES_URL = "https://aka.ms/ftwp/dataset.zip"


def download(url, filename=None, force=False):
    filename = filename or url.split('/')[-1]

    if os.path.isfile(filename) and not force:
        return filename

    def _report_download_status(chunk_id, max_chunk_size, total_size):
        size = chunk_id * max_chunk_size / 1024**2
        size_total = total_size / 1024**2
        unit = "Mb"
        if size <= size_total:
            print("{:.1f}{unit} / {:.1f}{unit}".format(size, size_total, unit=unit), end="\r")

    filename, _ = urllib.request.urlretrieve(url, filename, _report_download_status)
    return filename


def extract_games(zip_filename, dst):
    zipped_file = zipfile.ZipFile(zip_filename)
    filenames_to_extract = [f for f in zipped_file.namelist() if f.endswith(".z8") or f.endswith(".json")]

    subdirs = {
        "train": pjoin(dst, "train"),
        "valid": pjoin(dst, "valid"),
        "test": pjoin(dst, "test"),
    }
    for d in subdirs.values():
        if not os.path.isdir(d):
            os.makedirs(d)

    print("Extracting...")
    extracted_files = []
    for filename in tqdm.tqdm(filenames_to_extract):
        subdir = subdirs[os.path.basename(os.path.dirname(filename))]
        out_file = pjoin(subdir, os.path.basename(filename))
        extracted_files.append(out_file)
        if os.path.isfile(out_file):
            continue

        data = zipped_file.read(filename)
        with open(out_file, "wb") as f:
            f.write(data)

    return extracted_files


def collect_data_from_game(gamefile, seed, branching_depth):
    tokenizer = spacy.load('en', disable=['ner', 'parser', 'tagger'])
    rng = np.random.RandomState(seed)

    # Ignore the following commands.
    commands_to_ignore = ["look", "examine", "inventory"]

    env_infos = textworld.EnvInfos(description=True, location=True, facts=True, last_action=True,
                                   admissible_commands=True, game=True, extras=["walkthrough"])
    env = textworld.start(gamefile, env_infos)
    env = textworld.envs.wrappers.Filter(env)

    obs, infos = env.reset()
    walkthrough = infos["extra.walkthrough"]

    # Make sure we start with listing the inventory.
    if walkthrough[0] != "inventory":
        walkthrough = ["inventory"] + walkthrough

    # Add 'restart' command as a way to indicate the beginning of the game.
    walkthrough = ["restart"] + walkthrough

    dataset = []

    done = False
    facts_seen = set()
    for i, cmd in enumerate(walkthrough):
        last_facts = facts_seen
        if i > 0:  # != "restart"
            obs, _, done, infos = env.step(cmd)

        facts_seen = process_facts(last_facts, infos["game"], infos["facts"], infos["last_action"], cmd)

        dataset += [{
            "game": os.path.basename(gamefile),
            "step": (i, 0),
            "observation": preproc(obs, tokenizer=tokenizer),
            "previous_action": cmd.lower(),
            "target_commands": sorted(gen_graph_commands(facts_seen - last_facts, cmd="add")
                                      + gen_graph_commands(last_facts - facts_seen, cmd="delete")),
            "previous_graph_seen": sorted(serialize_facts(last_facts)),
            "graph_seen": sorted(serialize_facts(facts_seen)),
        }]

        if done:
            break  # Stop collecting data if game is done.

        # Fork the current game & seen facts.
        env_ = env.copy()
        facts_seen_ = facts_seen

        # Then, take N random actions.
        for j in range(1, branching_depth + 1):
            commands = [c for c in infos["admissible_commands"]
                        if ((c == "examine cookbook" or c.split()[0] not in commands_to_ignore)
                            and (i + 1) != len(walkthrough) and c != walkthrough[i + 1])]

            if len(commands) == 0:
                break

            cmd_ = rng.choice(commands)
            obs, _, done, infos = env_.step(cmd_)

            if done:
                break  # Stop collecting data if game is done.

            last_facts_ = facts_seen_
            facts_seen_ = process_facts(last_facts_, infos["game"], infos["facts"], infos["last_action"], cmd_)

            dataset += [{
                "game": os.path.basename(gamefile),
                "step": (i, j),
                "observation": preproc(obs, tokenizer=tokenizer),
                "previous_action": cmd_.lower(),
                "target_commands": sorted(gen_graph_commands(facts_seen_ - last_facts_, cmd="add")
                                        + gen_graph_commands(last_facts_ - facts_seen_, cmd="delete")),
                "previous_graph_seen": sorted(serialize_facts(last_facts_)),
                "graph_seen": sorted(serialize_facts(facts_seen_)),
            }]

    return gamefile, dataset


def collect_data(gamefiles, args):
    print("Using {} processes.".format(args.nb_processes))
    desc = "Extracting data from {} games".format(len(gamefiles))
    pbar = tqdm.tqdm(total=len(gamefiles), desc=desc)

    outfile = open(args.output, "w")
    outfile.write("[\n")

    def _assemble_results(args):
        gamefile, data = args
        pbar.set_postfix_str(gamefile)
        pbar.update()
        outfile.write(",\n".join(json.dumps(d) for d in data) + ",\n")

    if args.nb_processes > 1:
        pool = multiprocessing.Pool(args.nb_processes)
        results = []
        for i, gamefile in enumerate(gamefiles):
            seed = args.seed + i
            result = pool.apply_async(collect_data_from_game, (gamefile, seed, args.branching_depth), callback=_assemble_results)
            results.append(result)

        for result in results:
            result.get()

        pool.close()
        pool.join()

    else:
        for i, gamefile in enumerate(gamefiles):
            seed = args.seed + i
            data = collect_data_from_game(gamefile, seed, args.branching_depth)
            _assemble_results(data)

    pbar.close()
    outfile.seek(outfile.tell() - 2, os.SEEK_SET)  # Overwrite last comma.
    outfile.write("\n]")
    outfile.close()


def build_argparser():
    parser = argparse.ArgumentParser()

    parser.add_argument("--output", default="dataset.json",
                        help="Path where to save the dataset (.json)")

    parser.add_argument("--nb-processes", type=int,
                        help="Number of CPUs to use. Default: all available.")

    parser.add_argument("--seed", type=int, default=20190423,
                        help="Seed for the random exploration.")
    parser.add_argument("--branching-depth", type=int, default=0,
                        help="Number of random commands for each transition in the walkthrough. Default: %(default)s.")

    parser.add_argument("--games-dir", default="./games/",
                        help="Folder where to extract the downloaded games.")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Overwrite existing files.")

    return parser


def main():
    parser = build_argparser()
    args = parser.parse_args()
    args.nb_processes = args.nb_processes or multiprocessing.cpu_count()

    if os.path.isfile(args.output) and not args.force:
        parser.error("{} already exists. Use -f to overwrite.".format(args.output))

    if not os.path.exists(args.games_dir):
        filename = download(GAMES_URL, filename=ZIP_FILENAME, force=args.force)
        extracted_files = extract_games(filename, dst=args.games_dir)
        gamefiles = [f for f in extracted_files if f.endswith(".z8")]
    else:
        gamefiles = [args.games_dir + f for f in os.listdir(args.games_dir) if f.endswith(".z8")]

    collect_data(gamefiles, args)


if __name__ == "__main__":
    main()
