""" Uplink and Downlink handling for communications layer

Downlink needs to happen in several stages. First, raw data is read from the adapter. This data is collected in a pool
and the pool is passed to a deframer that extracts frames from this pool. Frames are queued and sent to the ground
side where they are and passed into the ground side handler and onto the other GDS processes. Downlink handles multiple
streams of data the FSW downlink, and loopback data from the uplink adapter.

Uplink is the reverse, it pulls data in from the ground handler, frames it, and sends it up to the waiting FSW. Uplink
is represented by a single thread, as it is not dealing with multiple streams of data that need to be multiplexed.

"""
import logging
import threading
from queue import Empty, Full, Queue
import pyion

from fprime.common.models.serialize.numerical_types import U32Type

from fprime_gds.common.communication.adapters.base import BaseAdapter
from fprime_gds.common.communication.framing import FramerDeframer
from fprime_gds.common.communication.ground import GroundHandler
from fprime_gds.common.utils.data_desc_type import DataDescType

DW_LOGGER = logging.getLogger("downlink")
UP_LOGGER = logging.getLogger("uplink")


class Downlinker:
    """Encapsulates communication downlink functions

    Handles downlink creating two threads, one to read and deframe, and the other to send data out to the ground side
    of the system. It is composed of an adapter used to read from the interface, a deframer that is used to deframe
    incoming data, and a ground handler that is used to interact with the ground side of the system.

    Two threaded stages are used to multiplex between loopback data and FSW downlink data without the need to busy spin
    waiting for data.
    """

    def __init__(
        self, adapter: BaseAdapter, ground: GroundHandler, deframer: FramerDeframer
    ):
        """Initialize the downlinker

        Constructs a new downlinker object used to run the downlink and deframing operation.

        Args:
            adapter: adapter used to read raw data from the hardware connection
            ground: handles the ground side connection
            deframer: deframer used to deframe data from the communication format
        """
        self.running = True
        self.th_ground = None
        self.th_data = None
        self.adapter = adapter
        self.ground = ground
        self.deframer = deframer
        self.outgoing = Queue()

    def start(self):
        """Starts the downlink pipeline"""
        self.th_ground = threading.Thread(target=self.sending, name="DownlinkTTSGroundThread")
        self.th_ground.daemon = True
        self.th_ground.start()
        self.th_data = threading.Thread(target=self.deframing, name="DownLinkDeframingThread")
        self.th_data.daemon = True
        self.th_data.start()

    def deframing(self):
        """Deframing stage of downlink

        Reads in data from the raw adapter and runs the deframing. Collects data in a pool and continually runs
        deframing against it where possible. Then appends new frames into the outgoing queue.
        """
        pool = b""
        while self.running:
            # Blocks until data is available, but may still return b"" if timeout
            pool += self.adapter.read()
            frames, pool = self.deframer.deframe_all(pool, no_copy=True)
            try:
                for frame in frames:
                    self.outgoing.put_nowait(frame)
            except Full:
                DW_LOGGER.warning("GDS ground queue full, dropping frame")

    def sending(self):
        """Outgoing stage of downlink

        Adapts the downlink adapter to the rest of the GDS system by draining the outgoing queue and sending those
        packets to the rest of the GDS. This uses the ground send_all method.
        """
        while self.running:
            frames = []
            try:
                # Blocking read of at least one frame, then drain the entire queue
                frames.append(self.outgoing.get(timeout=0.500))
                while not self.outgoing.empty():
                    frames.append(self.outgoing.get_nowait())
            except Empty:
                pass
            self.ground.send_all(frames)

    def stop(self):
        """Stop the thread depends will close the ground resource which may be blocking"""
        self.running = False

    def join(self):
        """Join on the ending threads"""
        for thread in [self.th_data, self.th_ground]:
            if thread is not None:
                thread.join()

    def add_loopback_frame(self, frame):
        """Adds a frame to loopback to ground

        Some uplink processes are virtualized on the ground, and thus must loopback packets. This is used for data
        handshaking that the FSW may not support.

        Args:
            frame: frame to loopback to ground
        """
        try:
            self.outgoing.put_nowait(frame)
        except Full:
            DW_LOGGER.warning("GDS ground queue full, dropping loopback frame")


class DtnDownlinker(Downlinker):

    EID_NAME      = 'ipn:3.1'
    DEST_EID_NAME = 'ipn:2.1'

    def __init__(
        self,
        adapter: BaseAdapter,
        ground: GroundHandler,
        deframer: FramerDeframer
    ):
        super().__init__(adapter, ground, deframer)
        self.th_dtn_deframe = None

        # Create a proxy to node 1 and attach to it
        proxy = pyion.get_bp_proxy(1)

        # Listen to 'ipn:3.1' for incoming data
        self.eid = proxy.bp_open(self.EID_NAME) # TODO use as part of `with ... as eid` clause

    def start(self):
        super().start()
        self.th_dtn_deframe = threading.Thread(target=self._dtn_deframe, name="DtnDeframeThread")
        self.th_dtn_deframe.daemon = True
        self.th_dtn_deframe.start()

    def join(self):
        super().join()
        if self.th_dtn_deframe is not None:
            self.th_dtn_deframe.join()

    def _dtn_deframe(self):
        try:
            while self.running:
                bundle_frame = self.eid.bp_receive()
                bundle_frame_str = ''.join(format(x, '02x') for x in bundle_frame)
                #print('[GDS] BP deframe: {}'.format(bundle_frame_str))
                try:
                    self.outgoing.put_nowait(bundle_frame)
                except Full:
                    DW_LOGGER.warning("GDS ground queue full, dropping frame")
        except OSError:
            if self.running:
                raise

    def deframing(self):
        pool = b""
        while self.running:
            # Blocks until data is available, but may still return b"" if timeout
            pool += self.adapter.read()
            ltp_frames, pool = self.deframer.deframe_all(pool, no_copy=True)
            for ltp_frame in ltp_frames:
                # Data has extra 4 bytes of data: 00 00 00 03 (`FW_PACKET_FILE`)
                # TODO does this break file downlink?
                ltp_frame = ltp_frame[4:]

                ltp_frame_str = ''.join(format(x, '02x') for x in ltp_frame)
                #print('[GDS] LTP deframe: {}'.format(ltp_frame_str))
                pyion.ltp.ltp_handle_inbound_segment(ltp_frame)


class Uplinker:
    """Uplinker used to pull data out of the ground layer and send to FSW

    Handles uplink by creating a single thread to read data from the ground layer, frame it, and pass it to the adapter
    to the hardware link to flight software. It is composed of an adapter used to write to the interface, a framer
    that is used to frame outgoing data, and a ground handler that is used to interact with the ground side of the
    system.

    Since there is one stream of data the uplink requires only one thread to run.

    """

    RETRY_COUNT = 3

    def __init__(
        self,
        adapter: BaseAdapter,
        ground: GroundHandler,
        framer: FramerDeframer,
        loopback: Downlinker,
    ):
        """Initializes the uplink class

        Initialize the uplink class using a hardware adapter, ground handler, and framer.
        loopback is used to virtualize the return packet handshake as FSW does not handle that.

        Args:
            adapter: hardware adapter used to write raw outgoing data bytes
            ground: ground handler receiving data from the ground system
            framer: framer used to frame wire bytes
            loopback: used to return handshake packets
        """
        self.th_uplink = None
        self.running = True
        self.ground = ground
        self.adapter = adapter
        self.loopback = loopback
        self.framer = framer

    def start(self):
        """Starts the uplink pipeline"""
        self.th_uplink = threading.Thread(target=self.uplink, name="UplinkThread")
        self.th_uplink.daemon = True
        self.th_uplink.start()

    def uplink(self):
        """Runs uplink of data from ground to FSW

        Primary stage of the uplink process, reads data from the ground adapter, and passes the rest of the data to the
        framer, and then onto the adapter to send to FSW. Uplink also generates handshake packets as the current FSW
        does not generate handshake packets.
        """
        try:
            while self.running:
                packets = self.ground.receive_all()
                for packet in [
                    packet
                    for packet in packets
                    if packet is not None and len(packet) > 0
                ]:
                    framed = self.framer.frame(packet)
                    # Uplink handles synchronous retries
                    for retry in range(Uplinker.RETRY_COUNT):
                        if self.adapter.write(framed):
                            self.loopback.add_loopback_frame(
                                Uplinker.get_handshake(packet)
                            )
                            break
                    else:
                        UP_LOGGER.warning(
                            "Uplink failed to send %d bytes of data after %d retries",
                            len(framed),
                            Uplinker.RETRY_COUNT,
                        )
        # An OSError might occur during shutdown and is harmless. If we are not shutting down, this error should be
        # propagated up the stack.
        except OSError:
            if self.running:
                raise

    def stop(self):
        """Stop the thread depends will close the ground resource which may be blocking"""
        self.running = False

    def join(self):
        """Join on the ending threads"""
        if self.th_uplink is not None:
            self.th_uplink.join()

    @staticmethod
    def get_handshake(packet: bytes) -> bytes:
        """Gets a handshake raw frame from the last packet

        Creates a handshake raw-frame by repeating the contents of the last packet with a handshake descriptor at the
        front.

        Args:
            packet: packet to repeat back out as handshake

        Returns:
            handshake packet
        """
        return U32Type(DataDescType["FW_PACKET_HAND"].value).serialize() + packet


class DtnUplinker(Uplinker):

    EID_NAME      = 'ipn:3.1'
    DEST_EID_NAME = 'ipn:2.1'

    def __init__(
        self,
        adapter: BaseAdapter,
        ground: GroundHandler,
        framer: FramerDeframer,
        loopback: Downlinker,
    ):
        super().__init__(adapter, ground, framer, loopback)
        self.th_dtn_frame = None

        # Create a proxy to node 1 and attach to it
        proxy = pyion.get_bp_proxy(1)

        # Listen to 'ipn:3.1' for incoming data
        self.eid = proxy.bp_open(self.EID_NAME) # TODO use as part of `with ... as eid` clause

    def start(self):
        super().start()
        self.th_dtn_frame = threading.Thread(target=self._dtn_frame, name="DtnFrameThread")
        self.th_dtn_frame.daemon = True
        self.th_dtn_frame.start()

    def join(self):
        super().join()
        if self.th_dtn_frame is not None:
            self.th_dtn_frame.join()

    def _dtn_frame(self):
        # Create a proxy to node 1 and attach to it
        pyion.ltp.ltp_init(0)
        self.vspan = pyion.find_span(2) # TODO replace hardcoded remote engine ID

        try:
            while self.running:
                # This is a blocking call
                assert self.eid.is_open == True # TODO remove assertion
                with pyion.ltp.ltp_dequeue_outbound_segment(self.vspan) as ltp_frame:
                    #ltp_frame_str = ''.join(format(x, '02x') for x in ltp_frame)
                    #print('[GDS] LTP frame: {}'.format(ltp_frame_str))
                    framed = self.framer.frame(ltp_frame)

                    # Uplink handles synchronous retries
                    for retry in range(Uplinker.RETRY_COUNT):
                        if self.adapter.write(framed):
                            break
                    else:
                        UP_LOGGER.warning(
                            "Uplink failed to send %d bytes of data after %d retries",
                            len(framed),
                            Uplinker.RETRY_COUNT,
                        )
        except OSError:
            if self.running:
                raise

    def uplink(self):
        try:
            while self.running:
                packets = self.ground.receive_all()
                for packet in [
                    packet
                    for packet in packets
                    if packet is not None and len(packet) > 0
                ]:
                    print(f'[GDS] BP sending {len(packet)} bytes')
                    assert self.eid.is_open == True # TODO remove assertion
                    self.eid.bp_send(self.DEST_EID_NAME, bytes(packet))

                    # Intentionally sending premature loopback handshake.
                    # Could possibly track packet -> LTP segment to know
                    # if packet was truly sent on the other thread.
                    self.loopback.add_loopback_frame(Uplinker.get_handshake(packet))
        # An OSError might occur during shutdown and is harmless.
        # If we are not shutting down, this error should be propagated up the stack.
        except OSError:
            if self.running:
                raise
