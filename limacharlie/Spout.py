from gevent import monkey; monkey.patch_all()
from gevent.queue import Queue
import gevent.pool
import sys
import json
import requests
import os
import traceback
import uuid

from .utils import *

_CLOUD_KEEP_ALIVES = 60
_TIMEOUT_SEC = ( _CLOUD_KEEP_ALIVES * 2 ) + 1

class Spout( object ):
    '''Listener object to receive data (Events, Detects or Audit) from a limacharlie.io Organization in pull mode.'''

    def __init__( self, man, data_type, is_parse = True, max_buffer = 1024, inv_id = None, tag = None, cat = None ):
        '''Connect to limacharlie.io to start receiving data.

        Args:
            manager (limacharlie.Manager obj): a Manager to use for interaction with limacharlie.io.
            data_typer (str): the type of data received from the cloud as specified in Outputs (event, detect, audit).
            is_parse (bool): if set to True (default) the data will be parsed as JSON to native Python.
            max_buffer (int): the maximum number of messages to buffer in the queue.
            inv_id (str): only receive events marked with this investigation ID.
            tag (str): only receive Events from Sensors with this Tag.
            cat (str): only receive Detections of this Category.
        '''

        self._man = man
        self._oid = man._oid
        self._apiKey = man._secret_api_key
        self._data_type = data_type
        self._cat = cat
        self._tag = tag
        self._invId = inv_id
        self._is_parse = is_parse
        self._max_buffer = max_buffer
        self._dropped = 0

        self._isStop = False

        if self._data_type not in ( 'event', 'detect', 'audit' ):
            raise LcApiException( 'Invalid data type: %s' % self._data_type )

        # Setup internal structures.
        self.queue = Queue( maxsize = self._max_buffer )
        self._threads = gevent.pool.Group()

        # Connect to limacharlie.io.
        spoutParams = { 'api_key' : self._apiKey, 'type' : self._data_type }
        if inv_id is not None:
            spoutParams[ 'inv_id' ] = self._invId
        if tag is not None:
            spoutParams[ 'tag' ] = self._tag
        if cat is not None:
            spoutParams[ 'cat' ] = self._cat
        # Spouts work by doing a POST to the output.limacharlie.io service with the
        # OID, Secret Key and any Output parameters we want. This POST will return
        # us an HTTP 303 See Other with the actual URL where the output will be
        # created for us. We take note of this redirect URL so that if need to
        # reconnect later we don't need to re-do the POST again. The redirected URL
        # contains a path with a randomized value which is what we use a short term
        # shared secret to get the data stream since we are not limiting connections
        # by IP.
        self._hConn = requests.post( 'https://output.limacharlie.io/output/%s' % ( self._oid, ), 
                                     data = spoutParams, 
                                     stream = True, 
                                     allow_redirects = True, 
                                     timeout = _TIMEOUT_SEC )
        self._finalSpoutUrl = self._hConn.history[ 0 ].headers[ 'Location' ]
        self._threads.add( gevent.spawn( self._handleConnection ) )

    def shutdown( self ):
        '''Stop receiving data.'''
        
        self._isStop = True

        if self._hConn is not None:
            self._hConn.close()

        self._threads.join( timeout = 2 )

    def getDropped( self ):
        '''Get the number of messages dropped because queue was full.'''
        return self._dropped

    def resetDroppedCounter( self ):
        '''Reset the counter of dropped messages.'''
        self._dropped = 0

    def _handleConnection( self ):
        while not self._isStop:
            self._man._printDebug( "Stream started." )
            try:
                for line in self._hConn.iter_lines( chunk_size = 1024 * 1024 * 10 ):
                    try:
                        if self._is_parse:
                            line = json.loads( line )
                            # The output.limacharlie.io service also injects a
                            # few trace messages like keepalives and number of
                            # events dropped (if any) from the server (indicating
                            # we are too slow). We filter those out here.
                            if '__trace' in line:
                                if 'dropped' == line[ '__trace' ]:
                                    self._dropped += int( line[ 'n' ] )
                            else:
                                self.queue.put_nowait( line )
                        else:
                            self.queue.put_nowait( line )
                    except:
                        self._dropped += 1
            except Exception as e:
                if not self._isStop:
                    self._man._printDebug( "Stream closed: %s" % str( e ) )
                else:
                    self._man._printDebug( "Stream closed." )
            finally:
                self._man._printDebug( "Stream closed." )

            if not self._isStop:
                self._hConn = requests.get( self._finalSpoutUrl, 
                                            stream = True, 
                                            allow_redirects = False, 
                                            timeout = _TIMEOUT_SEC )

def _signal_handler():
    global sp
    _printToStderr( 'You pressed Ctrl+C!' )
    if sp is not None:
        sp.shutdown()
    sys.exit( 0 )

def _printToStderr( msg ):
    sys.stderr.write( str( msg ) + '\n' )

if __name__ == "__main__":
    import argparse
    import getpass
    import uuid
    import gevent
    import signal
    import limacharlie

    sp = None
    gevent.signal( signal.SIGINT, _signal_handler )

    parser = argparse.ArgumentParser( prog = 'limacharlie.io spout' )
    parser.add_argument( 'oid',
                         type = lambda x: str( uuid.UUID( x ) ),
                         help = 'the OID to authenticate as.' )
    parser.add_argument( 'data_type',
                         type = str,
                         help = 'the type of data to receive in spout, one of "event", "detect" or "audit".' )
    parser.add_argument( '-i', '--investigation-id',
                         type = str,
                         dest = 'inv_id',
                         default = None,
                         help = 'spout should only receive events marked with this investigation id.' )
    parser.add_argument( '-t', '--tag',
                         type = str,
                         dest = 'tag',
                         default = None,
                         help = 'spout should only receive events from sensors tagged with this tag.' )
    parser.add_argument( '-c', '--category',
                         type = str,
                         dest = 'cat',
                         default = None,
                         help = 'spout should only receive detections from this category.' )
    args = parser.parse_args()
    secretApiKey = getpass.getpass( prompt = 'Enter secret API key: ' )

    _printToStderr( "Registering..." )
    man = limacharlie.Manager( oid = args.oid, secret_api_key = secretApiKey )
    sp = limacharlie.Spout( man,
                            args.data_type, 
                            inv_id = args.inv_id,
                            tag = args.tag,
                            cat = args.cat )

    _printToStderr( "Starting to listen..." )
    while True:
        data = sp.queue.get()
        print( json.dumps( data, indent = 2 ) )

    _printToStderr( "Exiting." )