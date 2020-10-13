import signal

from multiplex.refs import REDRAW


def setup(viewer_events, loop):
    def sigwinch():
        viewer_events.send((REDRAW, None))

    loop.add_signal_handler(signal.SIGWINCH, sigwinch)


def restore(loop):
    loop.remove_signal_handler(signal.SIGWINCH)
