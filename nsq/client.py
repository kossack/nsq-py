'''A client for talking to NSQ'''

from . import connection
from . import logger
from . import exceptions
from .constants import HEARTBEAT
from .response import Response, Error
from .http import nsqlookupd, ClientException

import select
import threading


class Client(object):
    '''A client for talking to NSQ over a connection'''
    def __init__(self,
        lookupd_http_addresses=None, nsqd_tcp_addresses=None, topic=None,
        timeout=0.1, **identify):
        # If lookupd_http_addresses are provided, so must a topic be.
        if lookupd_http_addresses:
            assert topic

        # The options to send along with identify when establishing connections
        self._identify_options = identify
        # A mapping of (host, port) to our nsqd connection objects
        self._connections = {}
        # The select timeout
        self._timeout = timeout
        # Create clients for each of lookupd instances
        lookupd_http_addresses = lookupd_http_addresses or []
        self._lookupd = [
            nsqlookupd.Client(host) for host in lookupd_http_addresses]
        self._topic = topic

        self._nsqd_tcp_addresses = nsqd_tcp_addresses or []
        # A lock for manipulating our connections
        self._lock = threading.RLock()
        # And lastly, instantiate our connections
        self.check_connections()

    def discover(self, topic):
        '''Run the discovery mechanism'''
        producers = []
        for lookupd in self._lookupd:
            try:
                # Find all the current producers on this instance
                for producer in lookupd.lookup(topic)['data']['producers']:
                    producers.append(
                        (producer['broadcast_address'], producer['tcp_port']))
            except ClientException:
                logger.exception('Failed to query %s', lookupd)

        new = []
        for host, port in producers:
            conn = self._connections.get((host, port))
            if not conn:
                logger.info('Discovered %s:%s', host, port)
                new.append(self.connect(host, port))
            elif not conn.alive():
                logger.info('Reconnecting to %s:%s', host, port)
                conn.connect()
            else:
                logger.debug('Connection to %s:%s still alive', host, port)

        # And return all the new connections
        return [conn for conn in new if conn]

    def check_connections(self):
        '''Connect to all the appropriate instances'''
        if self._lookupd:
            self.discover(self._topic)

        # Make sure we're connected to all the prescribed hosts
        for hostspec in self._nsqd_tcp_addresses:
            host, port = hostspec.split(':')
            port = int(port)
            conn = self._connections.get((host, port), None)
            # If there is no connection to it, we have to try to connect
            if not conn:
                logger.info('Connecting to %s:%s', host, port)
                self.connect(host, port)
            elif not conn.alive():
                # If we've connected to it before, but it's no longer alive,
                # we'll have to make a decision about when to try to reconnect
                # to it, if we need to reconnect to it at all
                pass

    def connect(self, host, port):
        '''Connect to the provided host, port'''
        conn = connection.Connection(host, port, **self._identify_options)
        conn.setblocking(0)
        self.add(conn)
        return conn

    def connections(self):
        '''Safely return a list of all our connections'''
        with self._lock:
            return self._connections.values()

    def add(self, connection):
        '''Add a connection'''
        key = (connection.host, connection.port)
        with self._lock:
            if key not in self._connections:
                self._connections[key] = connection
                return connection
            else:
                return None

    def remove(self, connection):
        '''Remove a connection'''
        key = (connection.host, connection.port)
        with self._lock:
            found = self._connections.pop(key, None)
        try:
            self.close_connection(found)
        except Exception as exc:
            logger.warn('Failed to close %s: %s', connection, exc)
        return found

    def close_connection(self, connection):
        '''A hook for subclasses when connections are closed'''
        connection.close()

    def close(self):
        '''Close this client down'''
        map(self.remove, self.connections())

    def read(self):
        '''Read from any of the connections that need it'''
        # We'll check all living connections
        connections = [c for c in self.connections() if c.alive()]

        if not connections:
            return []

        # Not all connections need to be written to, so we'll only concern
        # ourselves with those that require writes
        writes = [c for c in connections if c.pending()]
        readable, writable, exceptable = select.select(
            connections, writes, connections, self._timeout)

        # If we returned because the timeout interval passed, log it and return
        if not (readable or writable or exceptable):
            logger.debug('Timed out...')
            return []

        responses = []
        # For each readable socket, we'll try to read some responses
        for conn in readable:
            try:
                for res in conn.read():
                    # We'll capture heartbeats and respond to them automatically
                    if (isinstance(res, Response) and res.data == HEARTBEAT):
                        logger.info('Sending heartbeat to %s', conn)
                        conn.nop()
                        continue
                    elif isinstance(res, Error):
                        nonfatal = (
                            exceptions.FinFailedException,
                            exceptions.ReqFailedException,
                            exceptions.TouchFailedException
                        )
                        if not isinstance(res.exception(), nonfatal):
                            # If it's not any of the non-fatal exceptions, then
                            # we have to close this connection
                            logger.error(
                                'Closing %s: %s', conn, res.exception())
                            self.close_connection(conn)
                    responses.append(res)
            except exceptions.NSQException:
                logger.exception('Failed to read from %s', conn)
                self.close_connection(conn)

        # For each writable socket, flush some data out
        for conn in writable:
            conn.flush()

        # For each connection with an exception, try to close it and remove it
        # from our connections
        for conn in exceptable:
            self.close_connection(conn)

        return responses
