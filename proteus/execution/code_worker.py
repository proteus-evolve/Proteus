"""Stdlib-only child for the candidate-code RPC protocol.

This runs as an isolated Python interpreter with an explicitly supplied candidate
import root. It is process separation, not an operating-system security sandbox.
"""

import importlib
import inspect
import json
import os
import random
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path


class PipeChannel:
    """RPC over Docker exec's stdin/stdout; candidate diagnostics use stderr."""

    def __init__(self):
        self.input = os.dup(0)
        self.output = os.dup(1)
        os.dup2(2, 1)

    def makefile(self, mode):
        return os.fdopen(os.dup(self.input), mode)

    def sendall(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.output, view):]

    def close(self):
        os.close(self.input)
        os.close(self.output)


def main():
    pipes = sys.argv[1] == '--stdio'
    if pipes and os.getpgrp() != os.getpid():
        os.setsid()
    channel = PipeChannel() if pipes else socket.socket(fileno=int(sys.argv[1]))
    stream = channel.makefile('rb')
    limit = int(sys.argv[2])
    parent_pid, deadline = (os.getppid(), time.monotonic() + float(sys.argv[3])) if pipes else (
        int(sys.argv[3]), float(sys.argv[4]))
    def watch_parent():
        while True:
            if os.getppid() != parent_pid or time.monotonic() >= deadline:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            time.sleep(.05)
    # A hard-exited host cannot leave a candidate burning CPU indefinitely.
    threading.Thread(target=watch_parent, daemon=True).start()
    lock = threading.Lock()
    ordinal = 0

    def send(value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(data) > limit:
            raise ValueError('candidate RPC frame exceeds the declared byte limit')
        channel.sendall(data)

    def receive():
        data = stream.readline(limit + 1)
        if not data or len(data) > limit or not data.endswith(b'\n'):
            raise ValueError('invalid or oversized RPC frame')
        return json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite RPC value')))

    def model(prompt):
        nonlocal ordinal
        if type(prompt) is not str or not prompt.strip():
            raise ValueError('model callback needs a nonempty text prompt')
        # Candidate threads may call concurrently; the public service remains serial.
        with lock:
            ordinal += 1
            send({'kind': 'model', 'ordinal': ordinal, 'prompt': prompt})
            reply = receive()
            if set(reply) != {'kind', 'ordinal', 'text'} or reply['kind'] != 'model_result' or reply['ordinal'] != ordinal:
                raise ValueError('model reply identity mismatch')
            return reply['text']

    try:
        request = receive()
        options, call = request['options'], request['call']
        random.seed(call['seed'])
        sys.path.insert(0, os.getcwd())
        target = Path.cwd().joinpath(*options['module'].split('.'))
        expected = target.with_suffix('.py')
        if not expected.is_file():
            expected = target/'__init__.py'
        if not expected.is_file():
            raise ValueError('entry module is not present in the loaded candidate')
        module = importlib.import_module(options['module'])
        if not getattr(module, '__file__', None) or Path(module.__file__).resolve() != expected.resolve():
            raise ValueError('imported module differs from the loaded candidate source')
        names = (*options['methods'], options['state_getter'], options['state_setter'])
        if options['class_name']:
            factory = getattr(module, options['class_name'])
        else:
            candidates = [value for value in vars(module).values() if inspect.isclass(value)
                          and value.__module__ == module.__name__ and not inspect.isabstract(value)
                          and all(callable(getattr(value, name, None)) for name in names)]
            if len(candidates) != 1:
                raise ValueError('candidate module must define exactly one class implementing the declared interface')
            factory = candidates[0]
        if not inspect.isclass(factory) or not all(callable(getattr(factory, name, None)) for name in names):
            raise ValueError('candidate class does not implement the declared interface')
        candidate = factory(model)
        if call['state']['initialized']:
            getattr(candidate, options['state_setter'])(call['state']['value'])
        output = getattr(candidate, call['method'])(*call['args'], **call['kwargs'])
        state = getattr(candidate, options['state_getter'])()
        send({'kind': 'result', 'output': output, 'state': state})
    except BaseException:
        try:
            send({'kind': 'error', 'diagnostic': traceback.format_exc()})
        except BaseException:
            pass
        return 1
    finally:
        stream.close()
        channel.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
