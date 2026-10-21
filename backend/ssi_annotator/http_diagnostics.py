"""Classify HTTP parser warnings without recording headers, cookies or video data."""
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import time

from . import config


def classify_prefix(data: bytes) -> str:
    # VS Code PersistentProtocol: type:u8, id:u32, ack:u32, length:u32.
    # A KeepAlive has type=9, id=0 and an empty payload. Inspect the
    # complete control frame, not merely its leading TAB byte.
    if (len(data) == 13 and data[:5] == b'\x09\x00\x00\x00\x00'
            and data[9:] == b'\x00\x00\x00\x00'):
        return 'vscode_keepalive_on_http'
    if data.startswith(b'\x16\x03'):
        return 'tls_on_plain_http'
    if data.startswith(b'SSH-'):
        return 'ssh_on_http'
    if data.startswith(b'\x05'):
        return 'socks5_on_http'
    if data.startswith(b'PRI * HTTP/2.0'):
        return 'http2_on_http1'
    if data.startswith((b'GET ',b'HEAD ',b'POST ',b'PUT ',b'DELETE ',b'PATCH ',b'OPTIONS ')):
        return 'malformed_http1'
    return 'unrecognized_protocol'


class ParserWarningFilter(logging.Filter):
    def __init__(self, emit):
        super().__init__()
        self.emit = emit

    def filter(self, record):
        if record.getMessage() != 'Invalid HTTP request received.':
            return True
        category = 'unrecognized_protocol'
        first_byte = None
        packet_size = None
        context = {}
        frame = inspect.currentframe()
        try:
            for _ in range(24):
                if frame is None:
                    break
                if frame.f_globals.get('__name__','').startswith('uvicorn.protocols.http.'):
                    data = frame.f_locals.get('data')
                    if isinstance(data,bytes):
                        category = classify_prefix(data)
                        first_byte = data[0] if data else None
                        packet_size = len(data)
                        protocol = frame.f_locals.get('self')
                        client = getattr(protocol,'client',None)
                        if isinstance(client,tuple) and len(client) == 2:
                            context['peer_port'] = client[1]
                            context['peer_loopback'] = client[0] in ('127.0.0.1','::1')
                        cycle = getattr(protocol,'cycle',None)
                        if cycle is not None:
                            path = getattr(cycle,'scope',{}).get('path','')
                            context['video_request'] = path.endswith('/video')
                            context['response_started'] = bool(getattr(cycle,'response_started',False))
                            context['response_complete'] = bool(getattr(cycle,'response_complete',False))
                        break
                frame = frame.f_back
        finally:
            del frame
        exception = sys.exc_info()[1]
        self.emit({'time':time.time(),'category':category,
                   'parser_error':type(exception).__name__ if exception is not None else None,
                   'first_byte':first_byte,'packet_size':packet_size,**context})
        record.msg = f'Invalid HTTP request received. [protocol={category}]'
        record.args = ()
        return True


def install():
    handler = RotatingFileHandler(config.cache_dir()/'http-protocol-errors.jsonl',maxBytes=128*1024,backupCount=1)
    handler.setFormatter(logging.Formatter('%(message)s'))
    def emit(event):
        handler.handle(logging.LogRecord('ssi.http',logging.WARNING,__file__,0,json.dumps(event),(),None))
    logger = logging.getLogger('uvicorn.error')
    classifier = ParserWarningFilter(emit)
    logger.addFilter(classifier)
    def close():
        logger.removeFilter(classifier)
        handler.close()
    return close
