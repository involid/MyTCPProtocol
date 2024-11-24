import socket
import time
from queue import PriorityQueue


class UDPBasedProtocol:
    def __init__(self, *, local_addr, remote_addr):
        self.udp_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.remote_addr = remote_addr
        self.udp_socket.bind(local_addr)

    def sendto(self, data):
        return self.udp_socket.sendto(data, self.remote_addr)

    def recvfrom(self, n):
        msg, addr = self.udp_socket.recvfrom(n)
        return msg

    def close(self):
        self.udp_socket.close()


class TCPSegment:
    service_len = 8 + 8 # seq + ack
    ack_timeout = 0.01

    def __init__(self, seq_number: int, ack_number: int, data: bytes):
        self.seq_number = seq_number
        self.ack_number = ack_number
        self.data = data
        self._sending_time = time.time()

    def dump(self) -> bytes:
        seq = self.seq_number.to_bytes(8, "big", signed=False)
        ack = self.ack_number.to_bytes(8, "big", signed=False)
        return seq + ack + self.data

    def update_sending_time(self):
        self._sending_time = time.time()

    @staticmethod
    def load(data: bytes) -> 'TCPSegment':
        seq = int.from_bytes(data[:8], "big", signed=False)
        ack = int.from_bytes(data[8:16], "big", signed=False)
        return TCPSegment(seq, ack, data[TCPSegment.service_len:])

    @property
    def expired(self):
        return time.time() - self._sending_time > self.ack_timeout

    def __lt__(self, other):
        return self.seq_number < other.seq_number


class MyTCPProtocol(UDPBasedProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mss = 1500                     # Maximum Segment Size
        self.window_size = self.mss * 12    # Window size
        self.ack_crit_lag = 20              # Maximum acknowledgment lag

        self._sent_bytes_n = 0              # Total bytes sent
        self._confirmed_bytes_n = 0         # Total bytes acknowledged
        self._received_bytes_n = 0          # Total bytes received
        self._send_window = PriorityQueue() # Segments sent but not yet acknowledged
        self._recv_window = PriorityQueue() # Segments received but not yet processed
        self._buffer = bytes()              # Buffer for received data

    def send(self, data: bytes) -> int:
        sent_data_len = 0
        lag = 0
        while (data or self._confirmed_bytes_n < self._sent_bytes_n) and (lag < self.ack_crit_lag):
            window_is_full = (self._sent_bytes_n - self._confirmed_bytes_n > self.window_size)
            if not window_is_full and data:
                # Prepare and send a new segment
                segment_size = min(self.mss, len(data))
                segment_data = data[:segment_size]
                segment = TCPSegment(self._sent_bytes_n, self._received_bytes_n, segment_data)
                sent_length = self._send_segment(segment)
                data = data[sent_length:]
                sent_data_len += sent_length
            else:
                # Wait for acknowledgments before sending more data
                if self._receive_segment(timeout=TCPSegment.ack_timeout):
                    lag = 0  # Reset lag on successful receive
                else:
                    lag += 1
            # Resend any unacknowledged segments if necessary
            self._resend_earliest_segment()
        return sent_data_len

    def recv(self, n: int) -> bytes:
        data = self._buffer[:n]
        self._buffer = self._buffer[n:]
        while len(data) < n:
            if not self._receive_segment():
                break
            additional_data = self._buffer[:n - len(data)]
            data += additional_data
            self._buffer = self._buffer[len(additional_data):]
        return data

    def _receive_segment(self, timeout: float = None) -> bool:
        # receive a segment.
        self.udp_socket.settimeout(timeout)
        try:
            segment_data = self.recvfrom(self.mss + TCPSegment.service_len)
            segment = TCPSegment.load(segment_data)
        except (socket.timeout, socket.error):
            return False

        if len(segment.data) > 0:
            # Process received data segment
            self._recv_window.put((segment.seq_number, segment))
            self._process_recv_window()
            # Send acknowledgment
            ack_segment = TCPSegment(self._sent_bytes_n, self._received_bytes_n, b'')
            self._send_segment(ack_segment)

        self._confirmed_bytes_n = segment.ack_number
        self._process_send_window()
        return True

    def _send_segment(self, segment: TCPSegment) -> int:
        self.udp_socket.settimeout(None)
        total_sent = self.sendto(segment.dump())
        data_sent_len = total_sent - TCPSegment.service_len

        if segment.seq_number == self._sent_bytes_n:
            self._sent_bytes_n += data_sent_len

        if data_sent_len > 0:
            segment.data = segment.data[:data_sent_len]
            segment.update_sending_time()
            self._send_window.put((segment.seq_number, segment))
        return data_sent_len

    def _process_recv_window(self):
        # process received segments in order.
        while not self._recv_window.empty():
            seq_number, segment = self._recv_window.get()
            if segment.seq_number == self._received_bytes_n:
                # In-order segment; add data to buffer
                self._buffer += segment.data
                self._received_bytes_n += len(segment.data)
            elif segment.seq_number > self._received_bytes_n:
                # Out-of-order segment; re-queue it
                self._recv_window.put((seq_number, segment))
                break

    def _process_send_window(self):
        # remove acknowledged segments from the send window.
        while not self._send_window.empty():
            seq_number, segment = self._send_window.queue[0]
            if seq_number < self._confirmed_bytes_n:
                self._send_window.get()
            else:
                break

    def _resend_earliest_segment(self):
        # resend the earliest unacknowledged segment if expired.
        if not self._send_window.empty():
            seq_number, segment = self._send_window.queue[0]
            if segment.expired:
                self._send_window.get()
                self._send_segment(segment)

    def close(self):
        super().close()
