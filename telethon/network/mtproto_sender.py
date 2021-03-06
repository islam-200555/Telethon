"""
This module contains the class used to communicate with Telegram's servers
encrypting every packet, and relies on a valid AuthKey in the used Session.
"""
import asyncio
import logging
from asyncio import Event

from .. import helpers, utils
from ..errors import (
    BadMessageError, InvalidChecksumError, BrokenAuthKeyError,
    rpc_message_to_error
)
from ..extensions import BinaryReader
from ..tl import TLMessage, MessageContainer, GzipPacked
from ..tl.all_tlobjects import tlobjects
from ..tl.functions.auth import LogOutRequest
from ..tl.types import (
    MsgsAck, Pong, BadServerSalt, BadMsgNotification, FutureSalts,
    MsgNewDetailedInfo, MsgDetailedInfo, MsgsStateReq, MsgResendReq,
    MsgsAllInfo, MsgsStateInfo, RpcError
)

__log__ = logging.getLogger(__name__)


class MtProtoSender:
    """
    MTProto Mobile Protocol sender
    (https://core.telegram.org/mtproto/description).

    Note that this class is not thread-safe, and calling send/receive
    from two or more threads at the same time is undefined behaviour.
    Rationale:
        a new connection should be spawned to send/receive requests
        in parallel, so thread-safety (hence locking) isn't needed.
    """

    def __init__(self, session, connection, loop=None):
        """
        Initializes a new MTProto sender.

        :param session:
            the Session to be used with this sender. Must contain the IP and
            port of the server, salt, ID, and AuthKey,
        :param connection:
            the Connection to be used.
        :param loop:
            the asyncio loop to be used, or the default one.
        """
        self.session = session
        self.connection = connection
        self._loop = loop if loop else asyncio.get_event_loop()

        # If  we're  invoking something from an  update thread  but we're also
        # receiving other request from the main thread (e.g. an update arrives
        # and we need to process it)  we must  ensure that only one is calling
        # receive at a given moment, since the receive step is fragile.
        self._read_lock = asyncio.Lock(loop=self._loop)
        self._write_lock = asyncio.Lock(loop=self._loop)

        # Requests (as msg_id: Message) sent waiting to be received
        self._pending_receive = {}

    async def connect(self):
        """Connects to the server."""
        await self.connection.connect(self.session.server_address, self.session.port)

    def is_connected(self):
        """
        Determines whether the sender is connected or not.

        :return: true if the sender is connected.
        """
        return self.connection.is_connected()

    def disconnect(self, clear_pendings=True):
        """Disconnects from the server."""
        __log__.info('Disconnecting MtProtoSender...')
        self.connection.close()
        if clear_pendings:
            self._clear_all_pending()

    # region Send and receive

    async def send(self, requests, ordered=False):
        """
        Sends the specified TLObject(s) (which must be requests),
        and acknowledging any message which needed confirmation.

        :param requests: the requests to be sent.
        :param ordered: whether the requests should be invoked in the
                        order in which they appear or they can be executed
                        in arbitrary order in the server.
        :return: a list of msg_ids which are correspond to sent requests.
        """
        if not utils.is_list_like(requests):
            requests = (requests,)

        # Prepare the event of every request
        for r in requests:
            if r.confirm_received is None:
                r.confirm_received = Event(loop=self._loop)
            else:
                r.confirm_received.clear()

        if ordered:
            requests = iter(requests)
            messages = [TLMessage(self.session, next(requests))]
            for r in requests:
                messages.append(TLMessage(self.session, r,
                                          after_id=messages[-1].msg_id))
        else:
            messages = [TLMessage(self.session, r) for r in requests]

        self._pending_receive.update({m.msg_id: m for m in messages})
        msg_ids = [m.msg_id for m in messages]

        __log__.debug('Sending requests with IDs: %s', ', '.join(
            '{}: {}'.format(m.request.__class__.__name__, m.msg_id)
            for m in messages
        ))

        if len(messages) == 1:
            message = messages[0]
        else:
            message = TLMessage(self.session, MessageContainer(messages))
            # On bad_msg_salt errors, Telegram will reply with the ID of
            # the container and not the requests it contains, so in case
            # this happens we need to know to which container they belong.
            for m in messages:
                m.container_msg_id = message.msg_id

        await self._send_message(message)
        return msg_ids

    def forget_pendings(self, msg_ids):
        for msg_id in msg_ids:
            if msg_id in self._pending_receive:
                del self._pending_receive[msg_id]

    async def _send_acknowledge(self, msg_id):
        """Sends a message acknowledge for the given msg_id."""
        await self._send_message(TLMessage(self.session, MsgsAck([msg_id])))

    async def receive(self, updates_handler):
        """
        Receives a single message from the connected endpoint.

        This method returns nothing, and will only affect other parts
        of the MtProtoSender such as the updates callback being fired
        or a pending request being confirmed.

        Any unhandled object (likely updates) will be passed to
        update_state.process(TLObject).

        :param updates_handler:
            the handler that will process all the received
            Update and Updates objects.
        """
        await self._read_lock.acquire()
        try:
            body = await self.connection.recv()
        except (BufferError, InvalidChecksumError):
            # TODO BufferError, we should spot the cause...
            # "No more bytes left"; something wrong happened, clear
            # everything to be on the safe side, or:
            #
            # "This packet should be skipped"; since this may have
            # been a result for a request, invalidate every request
            # and just re-invoke them to avoid problems
            __log__.exception('Error while receiving server response. '
                              '%d pending request(s) will be ignored',
                              len(self._pending_receive))
            self._clear_all_pending()
            return
        finally:
            self._read_lock.release()

        message, remote_msg_id, remote_seq = self._decode_msg(body)
        with BinaryReader(message) as reader:
            await self._process_msg(remote_msg_id, remote_seq, reader, updates_handler)

    # endregion

    # region Low level processing

    async def _send_message(self, message):
        """
        Sends the given encrypted through the network.

        :param message: the TLMessage to be sent.
        """
        await self._write_lock.acquire()
        try:
            await self.connection.send(helpers.pack_message(self.session, message))
        finally:
            self._write_lock.release()

    def _decode_msg(self, body):
        """
        Decodes the body of the payload received from the network.

        :param body: the body to be decoded.
        :return: a tuple of (decoded message, remote message id, remote seq).
        """
        if len(body) < 8:
            if body == b'l\xfe\xff\xff':
                raise BrokenAuthKeyError()
            else:
                raise BufferError("Can't decode packet ({})".format(body))

        with BinaryReader(body) as reader:
            return helpers.unpack_message(self.session, reader)

    async def _process_msg(self, msg_id, sequence, reader, updates_handler):
        """
        Processes the message read from the network inside reader.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the BinaryReader that contains the message.
        :param updates_handler: the handler to process Update and Updates objects.
        :return: true if the message was handled correctly, false otherwise.
        """
        # TODO Check salt, session_id and sequence_number

        code = reader.read_int(signed=False)
        reader.seek(-4)

        # These are a bit of special case, not yet generated by the code gen
        if code == 0xf35c6d01:  # rpc_result, (response of an RPC call)
            __log__.debug('Processing Remote Procedure Call result')
            await self._send_acknowledge(msg_id)
            return await self._handle_rpc_result(msg_id, sequence, reader)

        if code == MessageContainer.CONSTRUCTOR_ID:
            __log__.debug('Processing container result')
            return await self._handle_container(msg_id, sequence, reader, updates_handler)

        if code == GzipPacked.CONSTRUCTOR_ID:
            __log__.debug('Processing gzipped result')
            return await self._handle_gzip_packed(msg_id, sequence, reader, updates_handler)

        if code not in tlobjects:
            __log__.warning(
                'Unknown message with ID %d, data left in the buffer %s',
                hex(code), repr(reader.get_bytes()[reader.tell_position():])
            )
            return False

        obj = reader.tgread_object()
        __log__.debug('Processing %s result', type(obj).__name__)

        if isinstance(obj, Pong):
            return await self._handle_pong(msg_id, sequence, obj)

        if isinstance(obj, BadServerSalt):
            return await self._handle_bad_server_salt(msg_id, sequence, obj)

        if isinstance(obj, (MsgsStateReq, MsgResendReq)):
            # just answer we don't know anything
            return await self._handle_msgs_state_forgotten(msg_id, sequence, obj)

        if isinstance(obj, MsgsAllInfo):
            # not interesting now
            return True

        if isinstance(obj, BadMsgNotification):
            return await self._handle_bad_msg_notification(msg_id, sequence, obj)

        if isinstance(obj, MsgDetailedInfo):
            return await self._handle_msg_detailed_info(msg_id, sequence, obj)

        if isinstance(obj, MsgNewDetailedInfo):
            return await self._handle_msg_new_detailed_info(msg_id, sequence, obj)

        if isinstance(obj, MsgsAck):  # may handle the request we wanted
            # Ignore every ack request *unless* when logging out,
            # when it seems to only make sense. We also need to set a non-None
            # result since Telegram doesn't send the response for these.
            for msg_id in obj.msg_ids:
                r = self._pop_request_of_type(msg_id, LogOutRequest)
                if r:
                    r.result = True  # Telegram won't send this value
                    r.confirm_received.set()
                    __log__.debug('Confirmed %s through ack', type(r).__name__)

            return True

        if isinstance(obj, FutureSalts):
            r = self._pop_request(obj.req_msg_id)
            if r:
                r.result = obj
                r.confirm_received.set()
                __log__.debug('Confirmed %s through salt', type(r).__name__)

        # If the object isn't any of the above, then it should be an Update.
        self.session.process_entities(obj)
        await self._send_acknowledge(msg_id)
        if updates_handler:
            updates_handler(obj)

        return True

    # endregion

    # region Message handling

    def _pop_request(self, msg_id):
        """
        Pops a pending **request** from self._pending_receive.

        :param msg_id: the ID of the message that belongs to the request.
        :return: the request, or None if it wasn't found.
        """
        message = self._pending_receive.pop(msg_id, None)
        if message:
            return message.request

    def _pop_request_of_type(self, msg_id, t):
        """
        Pops a pending **request** from self._pending_receive.

        :param msg_id: the ID of the message that belongs to the request.
        :param t: the type of the desired request.
        :return: the request matching the type t, or None if it wasn't found.
        """
        message = self._pending_receive.get(msg_id, None)
        if message and isinstance(message.request, t):
            return self._pending_receive.pop(msg_id).request

    def _pop_requests_of_container(self, container_msg_id):
        """
        Pops pending **requests** from self._pending_receive.

        :param container_msg_id: the ID of the container.
        :return: the requests that belong to the given container. May be empty.
        """
        msgs = [msg for msg in self._pending_receive.values()
                if msg.container_msg_id == container_msg_id]

        requests = [msg.request for msg in msgs]
        for msg in msgs:
            self._pending_receive.pop(msg.msg_id, None)
        return requests

    def _clear_all_pending(self):
        """
        Clears all pending requests, and flags them all as received.
        """
        for r in self._pending_receive.values():
            r.request.confirm_received.set()
            __log__.info('Abruptly confirming %s', type(r).__name__)
        self._pending_receive.clear()

    async def _resend_request(self, msg_id):
        """
        Re-sends the request that belongs to a certain msg_id. This may
        also be the msg_id of a container if they were sent in one.

        :param msg_id: the ID of the request to be resent.
        """
        request = self._pop_request(msg_id)
        if request:
            await self.send(request)
            return
        requests = self._pop_requests_of_container(msg_id)
        if requests:
            await self.send(*requests)

    async def _handle_pong(self, msg_id, sequence, pong):
        """
        Handles a Pong response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the Pong.
        :return: true, as it always succeeds.
        """
        request = self._pop_request(pong.msg_id)
        if request:
            request.result = pong
            request.confirm_received.set()
            __log__.debug('Confirmed %s through pong', type(request).__name__)

        return True

    async def _handle_container(self, msg_id, sequence, reader, updates_handler):
        """
        Handles a MessageContainer response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the MessageContainer.
        :param updates_handler: handler to handle Update and Updates objects.
        :return: true, as it always succeeds.
        """
        __log__.debug('Handling container')
        for inner_msg_id, _, inner_len in MessageContainer.iter_read(reader):
            begin_position = reader.tell_position()

            # Note that this code is IMPORTANT for skipping RPC results of
            # lost requests (i.e., ones from the previous connection session)
            try:
                if not await self._process_msg(inner_msg_id, sequence, reader, updates_handler):
                    reader.set_position(begin_position + inner_len)
            except:
                # If any error is raised, something went wrong; skip the packet
                reader.set_position(begin_position + inner_len)
                raise

        return True

    async def _handle_bad_server_salt(self, msg_id, sequence, bad_salt):
        """
        Handles a BadServerSalt response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the BadServerSalt.
        :return: true, as it always succeeds.
        """
        self.session.salt = bad_salt.new_server_salt

        # "the bad_server_salt response is received with the
        # correct salt, and the message is to be re-sent with it"
        await self._resend_request(bad_salt.bad_msg_id)

        return True

    async def _handle_msgs_state_forgotten(self, msg_id, sequence, req):
        await self._send_message(TLMessage(self.session, MsgsStateInfo(msg_id, chr(1) * len(req.msg_ids))))
        return True

    async def _handle_bad_msg_notification(self, msg_id, sequence, bad_msg):
        """
        Handles a BadMessageError response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the BadMessageError.
        :return: true, as it always succeeds.
        """
        error = BadMessageError(bad_msg.error_code)
        __log__.warning('Read bad msg notification %s: %s', bad_msg, error)
        if bad_msg.error_code in (16, 17):
            # sent msg_id too low or too high (respectively).
            # Use the current msg_id to determine the right time offset.
            self.session.update_time_offset(correct_msg_id=msg_id)
            __log__.info('Attempting to use the correct time offset')
            await self._resend_request(bad_msg.bad_msg_id)
            return True
        elif bad_msg.error_code == 32:
            # msg_seqno too low, so just pump it up by some "large" amount
            # TODO A better fix would be to start with a new fresh session ID
            self.session.sequence += 64
            __log__.info('Attempting to set the right higher sequence')
            await self._resend_request(bad_msg.bad_msg_id)
            return True
        elif bad_msg.error_code == 33:
            # msg_seqno too high never seems to happen but just in case
            self.session.sequence -= 16
            __log__.info('Attempting to set the right lower sequence')
            await self._resend_request(bad_msg.bad_msg_id)
            return True
        else:
            raise error

    async def _handle_msg_detailed_info(self, msg_id, sequence, msg_new):
        """
        Handles a MsgDetailedInfo response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the MsgDetailedInfo.
        :return: true, as it always succeeds.
        """
        # TODO For now, simply ack msg_new.answer_msg_id
        # Relevant tdesktop source code: https://goo.gl/VvpCC6
        await self._send_acknowledge(msg_new.answer_msg_id)
        return True

    async def _handle_msg_new_detailed_info(self, msg_id, sequence, msg_new):
        """
        Handles a MsgNewDetailedInfo response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the MsgNewDetailedInfo.
        :return: true, as it always succeeds.
        """
        # TODO For now, simply ack msg_new.answer_msg_id
        # Relevant tdesktop source code: https://goo.gl/G7DPsR
        await self._send_acknowledge(msg_new.answer_msg_id)
        return True

    async def _handle_rpc_result(self, msg_id, sequence, reader):
        """
        Handles a RPCResult response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the RPCResult.
        :return: true if the request ID to which this result belongs is found,
                 false otherwise (meaning nothing was read).
        """
        reader.read_int(signed=False)  # code
        request_id = reader.read_long()
        inner_code = reader.read_int(signed=False)
        reader.seek(-4)

        __log__.debug('Received response for request with ID %d', request_id)
        request = self._pop_request(request_id)

        if inner_code == RpcError.CONSTRUCTOR_ID:  # RPC Error
            reader.seek(4)
            if self.session.report_errors and request:
                error = rpc_message_to_error(
                    reader.read_int(), reader.tgread_string(),
                    report_method=type(request).CONSTRUCTOR_ID
                )
            else:
                error = rpc_message_to_error(
                    reader.read_int(), reader.tgread_string()
                )

            if request:
                request.rpc_error = error
                request.confirm_received.set()

            __log__.debug('Confirmed %s through error %s',
                          type(request).__name__, error)
            # else TODO Where should this error be reported?
            # Read may be async. Can an error not-belong to a request?
            return True  # All contents were read okay

        elif request:
            __log__.debug('Reading request response')
            if inner_code == GzipPacked.CONSTRUCTOR_ID:
                with BinaryReader(GzipPacked.read(reader)) as compressed_reader:
                    request.on_response(compressed_reader)
            else:
                request.on_response(reader)

            self.session.process_entities(request.result)
            request.confirm_received.set()
            __log__.debug(
                'Confirmed %s through normal result %s',
                type(request).__name__, type(request.result).__name__
            )
            return True

        # If it's really a result for RPC from previous connection
        # session, it will be skipped by the handle_container().
        # For some reason this also seems to happen when downloading
        # photos, where the server responds with FileJpeg().
        def _try_read(r):
            try:
                return r.tgread_object()
            except Exception as e:
                return '(failed to read: {})'.format(e)

        if inner_code == GzipPacked.CONSTRUCTOR_ID:
            with BinaryReader(GzipPacked.read(reader)) as compressed_reader:
                obj = _try_read(compressed_reader)
        else:
            obj = _try_read(reader)

        __log__.warning(
            'Lost request (ID %d) with code %s will be skipped, contents: %s',
            request_id, hex(inner_code), obj
        )
        return False

    async def _handle_gzip_packed(self, msg_id, sequence, reader, updates_handler):
        """
        Handles a GzipPacked response.

        :param msg_id: the ID of the message.
        :param sequence: the sequence of the message.
        :param reader: the reader containing the GzipPacked.
        :param updates_handler: the handler to process Update and Updates objects.
        :return: the result of processing the packed message.
        """
        __log__.debug('Handling gzip packed data')
        with BinaryReader(GzipPacked.read(reader)) as compressed_reader:
            return await self._process_msg(msg_id, sequence, compressed_reader, updates_handler)

    # endregion
