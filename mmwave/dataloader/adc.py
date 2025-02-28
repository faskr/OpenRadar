# Copyright 2019 The OpenRadar Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import codecs
from curses import raw
import socket
import struct
from enum import Enum

import numpy as np
from pyparsing import nums


class CMD(Enum):
    RESET_FPGA_CMD_CODE = '0100'
    RESET_AR_DEV_CMD_CODE = '0200'
    CONFIG_FPGA_GEN_CMD_CODE = '0300'
    CONFIG_EEPROM_CMD_CODE = '0400'
    RECORD_START_CMD_CODE = '0500'
    RECORD_STOP_CMD_CODE = '0600'
    PLAYBACK_START_CMD_CODE = '0700'
    PLAYBACK_STOP_CMD_CODE = '0800'
    SYSTEM_CONNECT_CMD_CODE = '0900'
    SYSTEM_ERROR_CMD_CODE = '0a00'
    CONFIG_PACKET_DATA_CMD_CODE = '0b00'
    CONFIG_DATA_MODE_AR_DEV_CMD_CODE = '0c00'
    INIT_FPGA_PLAYBACK_CMD_CODE = '0d00'
    READ_FPGA_VERSION_CMD_CODE = '0e00'

    def __str__(self):
        return str(self.value)


# MESSAGE = codecs.decode(b'5aa509000000aaee', 'hex')
CONFIG_HEADER = '5aa5'
CONFIG_STATUS = '0000'
CONFIG_FOOTER = 'aaee'
ADC_PARAMS = {'chirps': 128,  # 32
              'rx': 4,
              'tx': 1,
              'samples': 256, #16384, # <- continuous stream
              'IQ': 1,
              'bytes': 4}
# STATIC
MAX_PACKET_SIZE = 4096
BYTES_IN_PACKET = 1456
# DYNAMIC
BYTES_IN_FRAME = (ADC_PARAMS['chirps'] * ADC_PARAMS['rx'] * ADC_PARAMS['tx'] *
                  ADC_PARAMS['IQ'] * ADC_PARAMS['samples'] * ADC_PARAMS['bytes']) # 524,288
BYTES_IN_FRAME_CLIPPED = (BYTES_IN_FRAME // BYTES_IN_PACKET) * BYTES_IN_PACKET # 524,160
PACKETS_IN_FRAME = BYTES_IN_FRAME / BYTES_IN_PACKET # 360.088
PACKETS_IN_FRAME_CLIPPED = BYTES_IN_FRAME // BYTES_IN_PACKET # 360
UINT16_IN_PACKET = BYTES_IN_PACKET // 2 # 728
UINT16_IN_FRAME = BYTES_IN_FRAME // 2 # 262,144


class DCA1000:
    """Software interface to the DCA1000 EVM board via ethernet.

    Attributes:
        static_ip (str): IP to receive data from the FPGA
        adc_ip (str): IP to send configuration commands to the FPGA
        data_port (int): Port that the FPGA is using to send data
        config_port (int): Port that the FPGA is using to read configuration commands from


    General steps are as follows:
        1. Power cycle DCA1000 and XWR1xxx sensor
        2. Open mmWaveStudio and setup normally until tab SensorConfig or use lua script
        3. Make sure to connect mmWaveStudio to the board via ethernet
        4. Start streaming data
        5. Read in frames using class

    Examples:
        >>> dca = DCA1000()
        >>> adc_data = dca.read(timeout=.1)
        >>> frame = dca.organize(adc_data, 128, 4, 256)

    """

    def __init__(self, static_ip='192.168.33.30', adc_ip='192.168.33.180',
                 data_port=4098, config_port=4096):
        # Save network data
        # self.static_ip = static_ip
        # self.adc_ip = adc_ip
        # self.data_port = data_port
        # self.config_port = config_port

        # Create configuration and data destinations
        self.cfg_dest = (adc_ip, config_port)
        self.cfg_recv = (static_ip, config_port)
        self.data_recv = (static_ip, data_port)

        self.data = []
        self.packet_count = []
        self.byte_count = []

        self.frame_buff = []

        self.curr_buff = None
        self.last_frame = None

        self.lost_packets = None

    def configure(self):
        """Initializes and connects to the FPGA

        Returns:
            None

        """
        # Create socket
        self.config_socket = socket.socket(socket.AF_INET,
                                           socket.SOCK_DGRAM,
                                           socket.IPPROTO_UDP)

        # Bind config socket to fpga
        self.config_socket.bind(self.cfg_recv)

        # SYSTEM_CONNECT_CMD_CODE
        # 5a a5 09 00 00 00 aa ee
        print(self._send_command(CMD.SYSTEM_CONNECT_CMD_CODE))

        # READ_FPGA_VERSION_CMD_CODE
        # 5a a5 0e 00 00 00 aa ee
        print(self._send_command(CMD.READ_FPGA_VERSION_CMD_CODE))

        # CONFIG_FPGA_GEN_CMD_CODE
        # 5a a5 03 00 06 00 01 02 01 02 03 1e aa ee
        print(self._send_command(CMD.CONFIG_FPGA_GEN_CMD_CODE, '0600', '01010102031e'))# 'c005350c0000'))

        # CONFIG_PACKET_DATA_CMD_CODE 
        # 5a a5 0b 00 06 00 c0 05 35 0c 00 00 aa ee
        print(self._send_command(CMD.CONFIG_PACKET_DATA_CMD_CODE, '0600', 'c005350c0000'))

        self.config_socket.close()

    def read_frames(self, leftover=[], num_frames=1, timeout=1):
        """ Read in a single frame via UDP

        Args:
            timeout (float): Time to wait for packet before moving on

        Returns:
            Full frame as array if successful, else None

        """
        # Create socket
        self.data_socket = socket.socket(socket.AF_INET,
                                         socket.SOCK_DGRAM,
                                         socket.IPPROTO_UDP)

        # Bind data socket to fpga
        self.data_socket.bind(self.data_recv)

        # Configure
        self.data_socket.settimeout(timeout)

        # Frame buffer
        uint16_preread = len(leftover)
        packets_left = num_frames * PACKETS_IN_FRAME - uint16_preread / UINT16_IN_PACKET
        ret_frame = np.zeros(num_frames * UINT16_IN_FRAME - uint16_preread, dtype=np.uint16)

        # Read first packet and check alignment
        packet_num, byte_count, packet_data = self._read_data_packet()
        packets_read = 1
        ret_frame[0:UINT16_IN_PACKET] = packet_data
        data_start_byte = byte_count - BYTES_IN_PACKET - 2*uint16_preread
        if (data_start_byte) % BYTES_IN_FRAME != 0:
            print("Warning: not reading from start of a frame; data misaligned")

        # Read the rest of the packets
        data_end_byte = data_start_byte + num_frames * BYTES_IN_FRAME
        while True:
            packet_num, byte_count, packet_data = self._read_data_packet()
            packets_read += 1
            if byte_count >= data_end_byte:
                next_frame_start = (data_end_byte - byte_count)/2
                ret_frame[packet_num - 1] = packet_data[0:next_frame_start]
                next_leftover = packet_data[next_frame_start:]
                self.lost_packets = int(packets_left) + 1 - packets_read
                self.data_socket.close()
                return leftover + ret_frame, next_leftover
            else:
                ret_frame[packet_num - 1] = packet_data

    def read(self, timeout=1):
        """ Read in a single frame via UDP

        Args:
            timeout (float): Time to wait for packet before moving on

        Returns:
            Full frame as array if successful, else None

        """
        # Create socket
        self.data_socket = socket.socket(socket.AF_INET,
                                         socket.SOCK_DGRAM,
                                         socket.IPPROTO_UDP)

        # Bind data socket to fpga
        self.data_socket.bind(self.data_recv)

        # Configure
        self.data_socket.settimeout(timeout)

        # Frame buffer
        ret_frame = np.zeros(UINT16_IN_FRAME, dtype=np.uint16)

        # Wait for start of next frame
        while True:
            packet_num, byte_count, packet_data = self._read_data_packet()
            print(byte_count)
            if byte_count % BYTES_IN_FRAME_CLIPPED == 0:
                packets_read = 1
                ret_frame[0:UINT16_IN_PACKET] = packet_data
                #ret_frame[0:len(packet_data)] = packet_data
                break

        # Read in the rest of the frame
        while True:
            packet_num, byte_count, packet_data = self._read_data_packet()
            packets_read += 1
            print(packets_read)
            print(byte_count)

            if byte_count % BYTES_IN_FRAME_CLIPPED == 0:
                self.lost_packets = PACKETS_IN_FRAME_CLIPPED - packets_read
                self.data_socket.close()
                print(packets_read)
                print(byte_count)
                return ret_frame

            curr_idx = ((packet_num - 1) % PACKETS_IN_FRAME_CLIPPED)
            try:
                ret_frame[curr_idx * UINT16_IN_PACKET:(curr_idx + 1) * UINT16_IN_PACKET] = packet_data
            except:
                pass

            if packets_read > PACKETS_IN_FRAME_CLIPPED:
                print(packets_read)
                packets_read = 0

    def _send_command(self, cmd, length='0000', body='', timeout=1):
        """Helper function to send a single commmand to the FPGA

        Args:
            cmd (CMD): Command code to send to the FPGA
            length (str): Length of the body of the command (if any)
            body (str): Body information of the command
            timeout (int): Time in seconds to wait for socket data until timeout

        Returns:
            str: Response message

        """
        # Create timeout exception
        self.config_socket.settimeout(timeout)

        # Create and send message
        resp = ''
        msg = codecs.decode(''.join((CONFIG_HEADER, str(cmd), length, body, CONFIG_FOOTER)), 'hex')
        try:
            self.config_socket.sendto(msg, self.cfg_dest)
            resp, addr = self.config_socket.recvfrom(MAX_PACKET_SIZE)
        except socket.timeout as e:
            print(e)
        return resp

    def _read_data_packet(self):
        """Helper function to read in a single ADC packet via UDP

        Returns:
            int: Current packet number, byte count of data that has already been read, raw ADC data in current packet

        """
        data, addr = self.data_socket.recvfrom(MAX_PACKET_SIZE)
        packet_num = struct.unpack('<1l', data[:4])[0]
        byte_count = struct.unpack('>Q', b'\x00\x00' + data[4:10][::-1])[0]
        packet_data = np.frombuffer(data[10:], dtype=np.uint16)
        return packet_num, byte_count, packet_data

    def _listen_for_error(self):
        """Helper function to try and read in for an error message from the FPGA

        Returns:
            None

        """
        self.config_socket.settimeout(None)
        msg = self.config_socket.recvfrom(MAX_PACKET_SIZE)
        if msg == b'5aa50a000300aaee':
            print('stopped:', msg)

    def _start_stream(self):
        """Helper function to send the start command to the FPGA
        
        Returns:
            str: Response Message

        """
        # Create socket
        self.config_socket = socket.socket(socket.AF_INET,
                                           socket.SOCK_DGRAM,
                                           socket.IPPROTO_UDP)

        # Bind config socket to fpga
        self.config_socket.bind(self.cfg_recv)

        ret = self._send_command(CMD.RECORD_START_CMD_CODE)

        self.config_socket.close()

        return ret

    def _stop_stream(self):
        """Helper function to send the stop command to the FPGA

        Returns:
            str: Response Message

        """
        # Create socket
        self.config_socket = socket.socket(socket.AF_INET,
                                           socket.SOCK_DGRAM,
                                           socket.IPPROTO_UDP)

        # Bind config socket to fpga
        self.config_socket.bind(self.cfg_recv)

        ret = self._send_command(CMD.RECORD_STOP_CMD_CODE)

        self.config_socket.close()

        return ret

    @staticmethod
    def organize_frames(raw_data, num_frames, chirps_per_frame, num_rx, samples_per_chirp, complexity):
        """Reorganizes raw ADC data into a full set of frames

        Args:
            raw_frame (ndarray): Data to format
            num_frames: Number of frames
            num_chirps: Number of chirps included in the frame
            num_rx: Number of receivers used in the frame
            num_samples: Number of ADC samples included in each chirp

        Returns:
            ndarray: Reformatted frame of raw data of shape (num_chirps, num_rx, num_samples)

        """
        raw_data = raw_data.astype(np.int16)
        raw_data = raw_data.reshape((num_frames, chirps_per_frame, samples_per_chirp, complexity, num_rx))
        raw_data = raw_data[:, :, :, 0, :] + 1j * raw_data[:, :, :, 1, :]
        raw_data = np.transpose(raw_data, (0, 1, 3, 2))
        return raw_data

    @staticmethod
    def organize(raw_frame, num_chirps, num_rx, num_samples):
        """Reorganizes raw ADC data into a full frame

        Args:
            raw_frame (ndarray): Data to format
            num_chirps: Number of chirps included in the frame
            num_rx: Number of receivers used in the frame
            num_samples: Number of ADC samples included in each chirp

        Returns:
            ndarray: Reformatted frame of raw data of shape (num_chirps, num_rx, num_samples)

        """
        raw_frame = raw_frame.astype(np.int16)
        raw_frame = raw_frame.reshape((num_chirps, num_samples, 2, num_rx))
        raw_frame = raw_frame[:, :, 0, :] + 1j * raw_frame[:, :, 1, :]
        raw_frame = np.transpose(raw_frame, (0, 2, 1))
        return raw_frame
