import logging
import asyncio
import socket
from types import SimpleNamespace
import uvicorn
from ssi_annotator.http_diagnostics import classify_prefix,ParserWarningFilter


def test_classify_protocol_without_recording_request_data():
    assert classify_prefix(b'\x16\x03\x01\x00')=='tls_on_plain_http'
    assert classify_prefix(b'PRI * HTTP/2.0\r\n')=='http2_on_http1'
    assert classify_prefix(b'\x05\x01\x00')=='socks5_on_http'
    assert classify_prefix(b'SSH-2.0-client')=='ssh_on_http'
    assert classify_prefix(b'GET /private')=='malformed_http1'


def test_vscode_keepalive_requires_complete_control_frame():
    heartbeat = b'\x09' + bytes(12)
    assert classify_prefix(heartbeat)=='vscode_keepalive_on_http'
    assert classify_prefix(heartbeat[:5]+b'\x00\x00\x00\x07'+heartbeat[9:])=='vscode_keepalive_on_http'
    for other in (heartbeat[:12],heartbeat+b'X',b'\x09'+b'X'*12,
                  heartbeat[:4]+b'\x01'+heartbeat[5:],heartbeat[:-1]+b'\x01'):
        assert classify_prefix(other)=='unrecognized_protocol'


def test_parser_context_correlates_control_frame_with_active_video_without_payload():
    events=[]
    namespace={'__name__':'uvicorn.protocols.http.test','f':ParserWarningFilter(events.append),
               'logging':logging,'__file__':__file__}
    exec('def data_received(self, data):\n'
         '    try: raise ValueError("private contents")\n'
         '    except ValueError:\n'
         '        f.filter(logging.LogRecord("uvicorn.error",30,__file__,0,"Invalid HTTP request received.",(),None))',namespace)
    protocol=SimpleNamespace(client=('127.0.0.1',54321),cycle=SimpleNamespace(
        scope={'path':'/api/private-case/segments/private-revision/video'},
        response_started=True,response_complete=False))
    namespace['data_received'](protocol,b'\x09'+bytes(12))
    event=events[0]
    assert event['category']=='vscode_keepalive_on_http'
    assert event['peer_port']==54321 and event['peer_loopback']
    assert event['video_request'] and event['response_started'] and not event['response_complete']
    assert 'private' not in str(event)


def test_warning_record_never_includes_cookies_or_parser_exception_contents():
    events=[];f=ParserWarningFilter(events.append)
    try:raise ValueError('secret request content')
    except ValueError:
        r=logging.LogRecord('uvicorn.error',logging.WARNING,__file__,0,'Invalid HTTP request received.',(),None)
        assert f.filter(r)
    assert events[0]['parser_error']=='ValueError'
    assert 'secret' not in str(events) and 'secret' not in r.getMessage()
    assert set(events[0])=={'time','category','parser_error','first_byte','packet_size'}


def test_keepalive_during_http_video_response_closes_the_stream():
    async def run():
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':206,
                        'headers':[(b'content-length',b'1048576'),(b'content-type',b'video/mp4')]})
            await send({'type':'http.response.body','body':b'ftyp','more_body':True})
            while (await receive())['type'] != 'http.disconnect':
                pass
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        address=listener.getsockname()
        server=uvicorn.Server(uvicorn.Config(app,http='httptools',lifespan='off',
                              log_config=None,access_log=False,timeout_graceful_shutdown=1))
        events=[]
        logger=logging.getLogger('uvicorn.error')
        classifier=ParserWarningFilter(events.append)
        logger.addFilter(classifier)
        task=asyncio.create_task(server.serve(sockets=[listener]))
        writer=None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
                reader,writer=await asyncio.open_connection(*address)
                writer.write(b'GET /api/cases/example/video HTTP/1.1\r\nHost: localhost\r\n\r\n')
                await writer.drain()
                headers=await reader.readuntil(b'\r\n\r\n')
                assert headers.startswith(b'HTTP/1.1 206')
                assert await reader.readexactly(4)==b'ftyp'
                writer.write(b'\x09'+bytes(12))
                await writer.drain()
                # Uvicorn writes 400 into the unfinished response, then closes
                # the TCP stream before its declared video length is sent.
                remainder=await reader.read()
                assert remainder.startswith(b'HTTP/1.1 400 Bad Request')
                assert len(remainder)<1048576-4
                event=events[-1]
                assert event['category']=='vscode_keepalive_on_http'
                assert event['parser_error']=='HttpParserInvalidMethodError'
                assert event['video_request'] and event['response_started'] and not event['response_complete']
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            server.should_exit=True
            try:
                await asyncio.wait_for(task,3)
            finally:
                logger.removeFilter(classifier)
                listener.close()
    asyncio.run(run())
