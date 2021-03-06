import mock

import uuid

from nsq import reader
from nsq import response

from common import FakeServerTest, IntegrationTest


class TestReader(FakeServerTest):
    '''Tests for our reader class'''
    def setUp(self):
        self.topic = 'foo-topic'
        self.channel = 'foo-channel'
        FakeServerTest.setUp(self)

    def connect(self):
        '''Return a connection'''
        return reader.Reader(self.topic, self.channel,
            nsqd_tcp_addresses=['localhost:12345'])

    def test_it_subscribes(self):
        '''It subscribes for newly-established connections'''
        with mock.patch.object(self.client, 'distribute_ready'):
            with mock.patch('nsq.reader.Client') as MockClient:
                MockClient.add.return_value = mock.Mock()
                self.client.add(None)
                MockClient.add.return_value.sub.assert_called_with(
                    self.topic, self.channel)

    def test_new_connections_rdy(self):
        '''Calls rdy(1) when connections are added'''
        connection = mock.Mock()
        with mock.patch('nsq.reader.Client') as MockClient:
            MockClient.add.return_value = connection
            self.client.add(connection)
            connection.rdy.assert_called_with(1)

    def test_it_checks_max_in_flight(self):
        '''Raises an exception if more connections than in-flight limit'''
        with mock.patch.object(self.client, '_max_in_flight', 0):
            self.assertRaises(NotImplementedError, self.client.distribute_ready)

    def test_it_distributes_ready(self):
        '''It distributes RDY with util.distribute'''
        with mock.patch('nsq.reader.distribute') as mock_distribute:
            counts = range(10)
            connections = [mock.Mock() for _ in counts]
            mock_distribute.return_value = zip(counts, connections)
            self.client.distribute_ready()
            for count, connection in zip(counts, connections):
                connection.rdy.assert_called_with(count)

    def test_it_ignores_dead_connections(self):
        '''It does not distribute RDY state to dead connections'''
        dead = mock.Mock()
        dead.alive.return_value = False
        alive = mock.Mock()
        alive.alive.return_value = True
        with mock.patch.object(
            self.client, 'connections', return_value=[alive, dead]):
            self.client.distribute_ready()
            self.assertTrue(alive.rdy.called)
            self.assertFalse(dead.rdy.called)

    def test_it_honors_Client_add(self):
        '''If the parent client doesn't add a connection, it ignores it'''
        with mock.patch('nsq.reader.Client') as MockClient:
            with mock.patch.object(
                self.client, 'distribute_ready') as mock_distribute:
                MockClient.add.return_value = None
                self.client.add(None)
                self.assertFalse(mock_distribute.called)

    def test_zero_ready(self):
        '''When a connection has ready=0, distribute_ready is invoked'''
        connection = self.client.connections()[0]
        with mock.patch.object(connection, 'ready', 0):
            self.assertTrue(self.client.needs_distribute_ready())

    def test_not_ready(self):
        '''When no connection has ready=0, distribute_ready is not invoked'''
        connection = self.client.connections()[0]
        with mock.patch.object(connection, 'ready', 10):
            self.assertFalse(self.client.needs_distribute_ready())

    def test_negative_ready(self):
        '''If clients have negative RDY values, distribute_ready is invoked'''
        connection = self.client.connections()[0]
        with mock.patch.object(connection, 'ready', -1):
            self.assertTrue(self.client.needs_distribute_ready())

    def test_low_ready(self):
        '''If clients have negative RDY values, distribute_ready is invoked'''
        connection = self.client.connections()[0]
        with mock.patch.object(connection, 'ready', 2):
            with mock.patch.object(connection, 'last_ready_sent', 10):
                self.assertTrue(self.client.needs_distribute_ready())

    def test_none_alive(self):
        '''We don't need to redistribute RDY if there are none alive'''
        with mock.patch.object(self.client, 'connections', return_value=[]):
            self.assertFalse(self.client.needs_distribute_ready())

    def test_read(self):
        '''Read checks if we need to distribute ready'''
        with mock.patch('nsq.reader.Client'):
            with mock.patch.object(
                self.client, 'needs_distribute_ready', return_value=True):
                with mock.patch.object(
                    self.client, 'distribute_ready') as mock_ready:
                    self.client.read()
                    mock_ready.assert_called_with()

    def test_read_not_ready(self):
        '''Does not redistribute ready if not needed'''
        with mock.patch('nsq.reader.Client'):
            with mock.patch.object(
                self.client, 'needs_distribute_ready', return_value=False):
                with mock.patch.object(
                    self.client, 'distribute_ready') as mock_ready:
                    self.client.read()
                    self.assertFalse(mock_ready.called)

    def test_iter(self):
        '''The client can be used as an iterator'''
        iterator = iter(self.client)
        message_id = uuid.uuid4().hex[0:16]
        packed = response.Message.pack(0, 0, message_id, 'hello')
        messages = [response.Message(None, None, packed) for _ in range(10)]
        with mock.patch.object(self.client, 'read', return_value=messages):
            found = [iterator.next() for _ in range(10)]
            self.assertEqual(messages, found)

    def test_iter_repeated_read(self):
        '''Repeatedly calls read in iterator mode'''
        iterator = iter(self.client)
        message_id = uuid.uuid4().hex[0:16]
        packed = response.Message.pack(0, 0, message_id, 'hello')
        messages = [response.Message(None, None, packed) for _ in range(10)]
        for message in messages:
            with mock.patch.object(self.client, 'read', return_value=[message]):
                self.assertEqual(iterator.next(), message)

    def test_skip_non_messages(self):
        '''Skips all non-messages'''
        iterator = iter(self.client)
        message_id = uuid.uuid4().hex[0:16]
        packed = response.Message.pack(0, 0, message_id, 'hello')
        messages = [response.Message(None, None, packed) for _ in range(10)]
        packed = response.Response.pack('hello')
        responses = [
            response.Response(None, None, packed) for _ in range(10)] + messages
        with mock.patch.object(self.client, 'read', return_value=responses):
            found = [iterator.next() for _ in range(10)]
            self.assertEqual(messages, found)

    def test_honors_max_rdy_count(self):
        '''Honors the max RDY count provided in an identify response'''
        with self.identify({'max_rdy_count': 10}):
            self.client.distribute_ready()
            self.assertEqual(self.client.connections()[0].ready, 10)


class TestReaderIntegration(IntegrationTest):
    '''Integration test for the Reader'''
    def setUp(self):
        IntegrationTest.setUp(self)
        self.reader = reader.Reader(
            self.topic, self.channel, nsqd_tcp_addresses=['localhost:4150'])

    def test_read(self):
        '''Can receive a message in a basic way'''
        self.nsqd.pub(self.topic, 'hello')
        message = iter(self.reader).next()
        self.assertEqual(message.body, 'hello')
