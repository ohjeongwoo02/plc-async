"""시험용 Modbus RTU 서버. socket:// 연결로 실제 PyModbus 클라이언트와 통신한다.

PyModbus 시험 서버는 없는 국번에 예외 응답을 보내므로, 실제 무응답을 재현하려고 직접 만들었다.
없는 국번의 요청에는 아무것도 보내지 않는다.
"""
import socket
import struct
import threading
import time

FRAME_SIZE = 8  # FC03·FC06 요청은 모두 8바이트다.


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


class RtuServer:
    def __init__(self, stations=(1,), value=0, late_write_delay=None):
        self.stations = set(stations)
        self.value = value
        self.late_write_delay = late_write_delay  # 지정하면 쓰기를 적용하지 않고 응답만 늦게 보낸다.
        self.requests = []
        self._clients = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.1)
        self._running = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self.url = f"socket://127.0.0.1:{self._listener.getsockname()[1]}"

    def __enter__(self):
        self._running = True
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._running = False
        self._thread.join()
        self._listener.close()
        self.drop_clients()

    def drop_clients(self):
        clients, self._clients = self._clients, []
        for client in clients:
            client.close()

    def writes(self, station=None):
        return [r[2] for r in self.requests if r[1] == 6 and station in (None, r[0])]

    def _accept(self):
        while self._running:
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            self._clients.append(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        buffer = b""
        try:
            while data := client.recv(256):
                buffer += data
                while len(buffer) >= FRAME_SIZE:
                    frame, buffer = buffer[:FRAME_SIZE], buffer[FRAME_SIZE:]
                    reply = self._answer(frame)
                    if reply:
                        client.sendall(reply + crc16(reply))
        except OSError:
            pass

    def _answer(self, frame):
        if crc16(frame[:6]) != frame[6:8]:
            return None
        station, function, address, field = struct.unpack(">BBHH", frame[:6])
        self.requests.append((station, function, field))
        if station not in self.stations or address != 0:
            return None
        if function == 3 and field == 1:
            return struct.pack(">BBBH", station, 3, 2, self.value)
        if function == 6 and self.late_write_delay:
            time.sleep(self.late_write_delay)
            return frame[:6]
        if function == 6:
            self.value = field
            return frame[:6]
        return None
