#!/usr/bin/env python3
"""
This file contains the requestor part of our application. There are three areas here:
1. Splitting the data into multiple tasks, each of which can be executed by a provider.
2. Defining what data needs to be transferred to and from each provider's VM.
3. Scheduling the tasks via a yagna node running locally.
"""

import argparse
import asyncio
from datetime import timedelta
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import AsyncIterable, Iterator

from yapapi import Executor, Task, WorkContext
from yapapi.log import enable_default_logger, log_event_repr, log_summary
from yapapi.package import vm

from worker import HASH_PATH, WORDS_PATH, RESULT_PATH

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument("--hash", type=Path, default=Path("hash.json"))
arg_parser.add_argument("--words", type=Path, default=Path("words-short.txt"))
arg_parser.add_argument("--workers", type=Path, default=2)

args = argparse.Namespace()

TASK_TIMEOUT = timedelta(minutes=10)


def data(dict_file: Path, chunk_count: int) -> Iterator[Task]:
    """Split input data into chunks, each one being a single `Task` object.

    A single provider may compute multiple `Task`s.
    Return an iterator of `Task` objects.
    """
    with dict_file.open() as f:
        lines = [line.strip() for line in f]

    chunk_size = math.ceil(len(lines) / chunk_count)

    for i in range(0, len(lines), chunk_size):
        chunk = lines[i : i + chunk_size]
        yield Task(data=chunk)


async def worker(context: WorkContext, tasks: AsyncIterable[Task]):
    """Prepare a sequence of steps which need to happen for a task to be computed.

    `WorkContext` is a utility which allows us to define a series of commands to
    interact with a provider.
    Tasks are provided from a common, asynchronous queue.
    The signature of this function cannot change, as it's used internally by `Executor`.
    """
    async for task in tasks:
        context.send_json(str(WORDS_PATH), task.data)
        context.send_file(str(args.hash), str(HASH_PATH))

        context.run("/golem/entrypoint/worker.py")

        output_file = NamedTemporaryFile()
        context.download_file(str(RESULT_PATH), output_file.name)

        # Pass the prepared sequence of steps to Executor
        yield context.commit()

        task.accept_result(result=json.load(output_file))
        output_file.close()


async def main():
    # Set of parameters for the VM run by each of the providers
    package = await vm.repo(
        image_hash="1e53d1f82b4c49b111196fcb4653fce31face122a174d9c60d06cf9a",
        min_mem_gib=1.0,
        min_storage_gib=2.0,
    )

    executor = Executor(
        package=package,
        budget=1,
        subnet_tag="devnet-beta.1",
        event_consumer=log_summary(log_event_repr),
        timeout=TASK_TIMEOUT,
    )

    result = ""
    async with executor:
        data_iterator = data(args.words, args.workers)
        async for task in executor.submit(worker, data_iterator):
            # Every task object we receive here represents a computed task
            print(f"task computed: {task}, result: {task.result}")

            if task.result:
                result = task.result

        if result:
            print(f"Found matching word: {result}")
        else:
            print("No matching words found.")


if __name__ == "__main__":
    args = arg_parser.parse_args()

    loop = asyncio.get_event_loop()
    task = loop.create_task(main())

    # yapapi debug logging to a file
    enable_default_logger(log_file="yapapi.log")

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        # Make sure Executor is closed gracefully before exiting
        task.cancel()
        loop.run_until_complete(task)
