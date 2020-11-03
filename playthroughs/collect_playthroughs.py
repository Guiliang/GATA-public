import os
import json
import argparse
import multiprocessing
from os.path import join as pjoin

import tqdm
import numpy as np
import spacy

import textworld
from textworld.logic import State, Rule, Proposition, Variable

from generic import process_facts, serialize_facts, gen_graph_commands
from generic import process_local_obs_facts, process_fully_obs_facts
from viz import show_kg


def collect_data_from_game(gamefile):
    # Ignore the following commands.
    commands_to_ignore = ["look", "examine", "inventory"]

    env_infos = textworld.EnvInfos(description=True, location=True, facts=True, last_action=True,
                                   admissible_commands=True, game=True, extras=["walkthrough"])
    env = textworld.start(gamefile, env_infos)

    infos = env.reset()
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
            infos, _, done = env.step(cmd)

        facts_local = process_local_obs_facts(infos["game"], infos["facts"], infos["last_action"], cmd)
        facts_seen = process_facts(last_facts, infos["game"], infos["facts"], infos["last_action"], cmd)
        facts_full = process_fully_obs_facts(infos["game"], infos["facts"])

        dataset += [{
            "game": os.path.basename(gamefile),
            "step": (i, 0),
            "action": cmd.lower(),
            "graph_local": sorted(serialize_facts(facts_local)),
            "graph_seen": sorted(serialize_facts(facts_seen)),
            "graph_full": sorted(serialize_facts(facts_full)),
        }]

        if done:
            break  # Stop collecting data if game is done.

        # Then, try all admissible commands.
        commands = [c for c in infos["admissible_commands"]
                    if ((c == "examine cookbook" or c.split()[0] not in commands_to_ignore)
                        and (i + 1) != len(walkthrough) and c != walkthrough[i + 1])]

        for j, cmd_ in enumerate(commands, start=1):
            env_ = env.copy()
            infos, _, done = env_.step(cmd_)

            facts_local_ = process_local_obs_facts(infos["game"], infos["facts"], infos["last_action"], cmd_)
            facts_seen_ = process_facts(facts_seen, infos["game"], infos["facts"], infos["last_action"], cmd_)
            facts_full_ = process_fully_obs_facts(infos["game"], infos["facts"])

            dataset += [{
                "game": os.path.basename(gamefile),
                "step": (i, j),
                "action": cmd_.lower(),
                "graph_local": sorted(serialize_facts(facts_local_)),
                "graph_seen": sorted(serialize_facts(facts_seen_)),
                "graph_full": sorted(serialize_facts(facts_full_)),
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
        for gamefile in gamefiles:
            result = pool.apply_async(collect_data_from_game, (gamefile,), callback=_assemble_results)
            results.append(result)

        for result in results:
            result.get()

        pool.close()
        pool.join()

    else:
        for i, gamefile in enumerate(gamefiles):
            data = collect_data_from_game(gamefile)
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

    parser.add_argument("games", nargs="+",
                        help="List of games (or folders containing games) to collect the playthroughs from.")

    parser.add_argument("-f", "--force", action="store_true",
                        help="Overwrite existing files.")

    return parser


def main():
    parser = build_argparser()
    args = parser.parse_args()
    args.nb_processes = args.nb_processes or multiprocessing.cpu_count()

    if os.path.isfile(args.output) and not args.force:
        parser.error("{} already exists. Use -f to overwrite.".format(args.output))

    gamefiles = []
    for e in args.games:
        if os.path.isdir(e):
            gamefiles += [pjoin(e, f) for f in os.listdir(e) if f.endswith(".json")]
        elif e.endswith(".json"):
            gamefiles.append(e)
        else:
            print("Ignoring non-JSON files '{}'".format(e))

    collect_data(gamefiles, args)


if __name__ == "__main__":
    main()
