import argparse
import asyncio
import io
import random
import threading
import time

import colors
from colors.colors import _color_code as cc


from multiplex import ansi
from multiplex.iterator import Iterator
from multiplex import Viewer
from multiplex.logging import init_logging
from multiplex.view_builder import ViewBuilder


def run_simple():
    async def text_generator(index):
        for i in range(num_iterations):
            output = f"iterator-number-#{index + 1}-({i + 1})|" * 7
            output = f"{output}\n"
            yield output
            await asyncio.sleep(random.random() / 10)
            await asyncio.sleep(0.5)
        yield "done"

    num_iterations = 2000
    num_iterators = 20
    iterators = [Iterator(text_generator(i), f"It #{i + 1}") for i in range(num_iterators)]
    return iterators


def run_colors():
    async def text_generator():
        for i in range(num_iterations):
            r = random.randint(0, 256)
            g = random.randint(0, 256)
            b = random.randint(0, 256)
            output = "".join(f'{colors.color(f"hello-{i}-", (r, g, b))}' for i in range(30))
            output = f"{output}\n"
            yield output
            await asyncio.sleep(random.random() / 10)
        yield "done"

    num_iterations = 2000
    num_iterators = 3
    iterators = [Iterator(text_generator, f"It #{i + 1}") for i in range(num_iterators)]
    return iterators


def run_dynamic():
    async def text_generator():
        for i in range(num_iterations):
            for j in range(10000):
                yield "hello"
                await asyncio.sleep(0.1)
                yield "\rgoodbye"
                await asyncio.sleep(0.1)
                yield "\r" + ansi.CLEAR_LINE
            yield "\n"
            await asyncio.sleep(random.random() / 10)
            await asyncio.sleep(0.5)
        yield "done"

    num_iterations = 2000
    num_iterators = 3
    iterators = [Iterator(text_generator, f"It #{i + 1}") for i in range(num_iterators)]
    return iterators


def run_style():
    async def text_generator():
        for i in range(num_iterations):

            fr = random.randint(0, 256)
            fg = random.randint(0, 256)
            fb = random.randint(0, 256)

            br = random.randint(0, 256)
            bg = random.randint(0, 256)
            bb = random.randint(0, 256)

            def code(*codes):
                return f'{ansi.CSI}{";".join(str(c) for c in codes)}m'

            reset = code(0)

            text_buffer = io.StringIO()
            for j in range(100):
                text_buffer.write(code(cc("red", 30)))
                text_buffer.write(code(cc("green", 40)))
                text_buffer.write(f"some text {j} ")
                text_buffer.write(code(cc((fr, fg, fb), 30)))
                text_buffer.write(code(cc((br, bg, bb), 40)))
                text_buffer.write(f"some text {j} ")
                text_buffer.write(code(3, 4, 7))
                text_buffer.write(f"some text {j} ")
                text_buffer.write(code(24, 9, 1))
                text_buffer.write(f"some text {j} ")
                text_buffer.write(reset)
            output = text_buffer.getvalue()
            output = f"{output}\n"
            yield output
            await asyncio.sleep(random.random() / 10)
            await asyncio.sleep(0.5)
        yield "done"

    num_iterations = 2000
    num_iterators = 3
    iterators = [Iterator(text_generator, f"It #{i + 1}") for i in range(num_iterators)]
    return iterators


def run_processes():
    cmds = ["gls -la --group-directories-first --color=always"]
    return [Iterator(cmds[0])]


def run_controller():
    builder = ViewBuilder()
    c1 = builder.new_controller("runner1")
    c2 = builder.new_controller("runner2")
    viewer = builder.build()

    async def runner(c):
        await asyncio.sleep(1)
        c.write("some data 1\n")
        await asyncio.sleep(1)
        c.write("some data 2\n")
        await asyncio.sleep(1)
        c.write("some data 2\n")
        await asyncio.sleep(1)
        c.write("some data 2\n")
        c.set_title(f"{c.title} [done]")
        c.collapse()

    future = asyncio.gather(runner(c1), runner(c2), viewer.run_async())
    try:
        asyncio.get_event_loop().run_until_complete(future)
    finally:
        viewer.restore()


def run_controller_thread_safe():
    builder = ViewBuilder()
    c1 = builder.new_controller("runner1", thread_safe=True)
    c2 = builder.new_controller("runner2", thread_safe=True)
    viewer = builder.build()

    def runner(c):
        time.sleep(1)
        c.write("some data 1\n")
        time.sleep(1)
        c.write("some data 2\n")
        time.sleep(1)
        c.write("some data 2\n")
        time.sleep(1)
        c.write("some data 2\n")
        c.set_title(f"{c.title} [done]")
        c.collapse()

    threads = [threading.Thread(target=runner, args=(c,)) for c in [c1, c2]]
    for t in threads:
        t.daemon = True
        t.start()
    viewer.run()


whats = {
    "simple": run_simple,
    "process": run_processes,
    "color": run_colors,
    "dyn": run_dynamic,
    "style": run_style,
    "control": run_controller,
    "tcontrol": run_controller_thread_safe,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("what")
    return parser.parse_args()


def main():
    try:
        init_logging()
        args = parse_args()
        fn = whats.get(args.what, run_simple)
        result = fn()
        if isinstance(result, list):
            viewer = Viewer(result, verbose=True)
            viewer.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
