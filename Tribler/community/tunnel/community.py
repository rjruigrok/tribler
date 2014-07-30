import time
import random

from traceback import print_exc
from collections import defaultdict

from twisted.internet import reactor
from twisted.internet.protocol import DatagramProtocol
from twisted.internet.task import LoopingCall

from Crypto.Util.number import bytes_to_long, long_to_bytes

from Tribler.Core.Utilities.encoding import encode, decode
from Tribler.community.tunnel import CIRCUIT_STATE_READY, CIRCUIT_STATE_EXTENDING, ORIGINATOR, \
                                     PING_INTERVAL, ENDPOINT
from Tribler.community.tunnel.conversion import TunnelConversion
from Tribler.community.tunnel.payload import CellPayload, CreatePayload, CreatedPayload, ExtendPayload, \
                                             ExtendedPayload, PongPayload, PingPayload
from Tribler.community.tunnel.routing import Circuit, Hop, RelayRoute
from Tribler.community.tunnel.tests.test_libtorrent import LibtorrentTest
from Tribler.community.tunnel.Socks5.server import Socks5Server
from Tribler.community.tunnel.crypto import TunnelCrypto, CryptoException

from Tribler.dispersy.authentication import NoAuthentication
from Tribler.dispersy.candidate import Candidate
from Tribler.dispersy.community import Community
from Tribler.dispersy.conversion import DefaultConversion
from Tribler.dispersy.destination import CandidateDestination
from Tribler.dispersy.distribution import DirectDistribution
from Tribler.dispersy.message import Message, DropMessage
from Tribler.dispersy.resolution import PublicResolution
from Tribler.dispersy.logger import get_logger
from Tribler.dispersy.util import call_on_reactor_thread
from Tribler.dispersy.requestcache import NumberCache, RandomNumberCache
from Tribler.Core.exceptions import OperationNotEnabledByConfigurationException

logger = get_logger(__name__)


class CircuitRequestCache(NumberCache):

    def __init__(self, community, circuit):
        super(CircuitRequestCache, self).__init__(community.request_cache, u"anon-circuit", circuit.circuit_id)
        self.community = community
        self.circuit = circuit

    def on_timeout(self):
        if self.circuit.state != CIRCUIT_STATE_READY:
            reason = 'timeout on CircuitRequestCache, state = %s, candidate = %s' % (self.circuit.state, self.circuit.first_hop)
            self.community.remove_circuit(self.number, reason)


class CreatedRequestCache(NumberCache):

    def __init__(self, community, circuit_id, candidate, candidates):
        super(CreatedRequestCache, self).__init__(community.request_cache, u"anon-created", circuit_id)
        self.circuit_id = circuit_id
        self.candidate = candidate
        self.candidates = candidates

    def on_timeout(self):
        pass


class PingRequestCache(RandomNumberCache):

    def __init__(self, community, circuit):
        super(PingRequestCache, self).__init__(community.request_cache, u"ping")
        self.circuit = circuit
        self.community = community

    @property
    def timeout_delay(self):
        return PING_INTERVAL + 5

    def on_timeout(self):
        if self.circuit.last_incoming < time.time() - self.timeout_delay:
            logger.debug("ForwardCommunity: no response on ping, circuit %d timed out", self.circuit.circuit_id)
            self.community.remove_circuit(self.circuit.circuit_id, 'ping timeout')


class TunnelExitSocket(DatagramProtocol):

    def __init__(self, circuit_id, destination, community):
        self.port = reactor.listenUDP(0, self)

        self.destination = destination
        self.circuit_id = circuit_id
        self.community = community
        self.ips = defaultdict(int)
        self.bytes_up = self.bytes_down = 0
        self.creation_time = time.time()

    def sendto(self, data, destination):
        if self.check_num_packets(destination, False):
            self.bytes_up += len(data)
            if TunnelConversion.could_be_utp(data):
                self.transport.write(data, destination)
            else:
                logger.error("TunnelCommunity: dropping non-utp packets from exit socket with circuit_id %d", self.circuit_id)

    def datagramReceived(self, data, source):
        if self.check_num_packets(source, True):
            self.bytes_down += len(data)
            if TunnelConversion.could_be_utp(data):
                self.community.tunnel_data_to_origin(self.circuit_id, self.destination, source, data)
            else:
                logger.error("TunnelCommunity: dropping non-utp packets to exit socket with circuit_id %d", self.circuit_id)

    def close(self):
        if self.port:
            self.port.stopListening()
            self.port = None

    def check_num_packets(self, ip, incoming):
        max_packets_without_reply = self.community.settings.max_packets_without_reply
        if self.ips[ip] >= (max_packets_without_reply + 1 if incoming else max_packets_without_reply):
            self.community.remove_exit_socket(self.circuit_id)
            logger.error("TunnelCommunity: too many packets to a destination without a reply, " \
                         "removing exit socket with circuit_id %d", self.circuit_id)
            return False

        if incoming:
            del self.ips[ip]
        else:
            self.ips[ip] += 1

        return True


class TunnelSettings:

    def __init__(self):
        self.circuit_length = 3
        self.crypto = TunnelCrypto()
        self.socks_listen_port = 1080
        self.min_circuits_for_session = 4

        self.max_circuits = 8
        self.max_relays_or_exits = 100

        self.max_time = 10 * 60
        self.max_time_inactive = 20
        self.max_traffic = 10 * 1024 * 1024

        self.max_packets_without_reply = 50

class RoundRobin(object):

    def __init__(self, community):
        self.community = community
        self.index = -1

    def has_options(self):
        return len(self.community.active_circuits) > 0

    def select(self):
        circuit_ids = sorted(self.community.active_circuits.keys())

        if not circuit_ids:
            return None

        self.index = (self.index + 1) % len(circuit_ids)
        circuit_id = circuit_ids[self.index]
        return self.community.active_circuits[circuit_id]

class TunnelCommunity(Community):

    def initialize(self, session=None, settings=None):
        super(TunnelCommunity, self).initialize()

        self.tribler_session = session
        self.data_prefix = "fffffffe".decode("HEX")
        self.circuits = {}
        self.directions = {}
        self.relay_from_to = {}
        self.relay_session_keys = {}
        self.waiting_for = set()
        self.exit_sockets = {}
        self.notifier = None
        self.made_anon_session = False
        self.selection_strategy = RoundRobin(self)

        self.settings = settings if settings else TunnelSettings()

        assert isinstance(self.settings.crypto, TunnelCrypto)
        assert self.settings.circuit_length > 0

        self.crypto.initialize(self)

        self._dispersy.endpoint.listen_to(self.data_prefix, self.on_data)
        self.register_task("do_circuits", LoopingCall(self.do_circuits)).start(5, now=True)
        self.register_task("do_ping", LoopingCall(self.do_ping)).start(PING_INTERVAL)

        if session:
            from Tribler.Core.CacheDB.Notifier import Notifier
            self.notifier = Notifier.getInstance()

            if session.get_libtorrent():
                self.libtorrent_test = LibtorrentTest(self, session)
                #if not self.libtorrent_test.has_completed_before():
                logger.debug("Scheduling Anonymous LibTorrent download")
                self.register_task("start_test", reactor.callLater(1, lambda : reactor.callInThread(self.libtorrent_test.start)))

        self.socks_server = Socks5Server(self, session.get_tunnel_community_socks5_listen_port() \
                                                                if session else self.settings.socks_listen_port)
        self.socks_server.start()

    @classmethod
    def get_master_members(cls, dispersy):
        # generated: Mon Jun 16 15:31:00 2014
        # curve: NID_sect571r1
        # len: 571 bits ~ 144 bytes signature
        # pub: 170 3081a7301006072a8648ce3d020106052b8104002703819200040380695ccdb2f64d6fc21f070cf151f3bd3e159609a642aaa023b3620d3
        # f452a4d6114a9031c1f1155fdc6c3d89689c02ec7205e306a1ea397d59a8e0056702d704d97d68a70a9b000e2b902d2ffb92107125d24d91cb771f11
        # cea88b157c8d6421eaecc1ddb9d6dad4a907373f08b3f1eb12438d290f282fe2a5cec9f2deb71b23a77e74a787caf9faf2a202adbf728
        # pub-sha1 f292e197efee5cd785c3f89d6e4826fd08c556f4
        #-----BEGIN PUBLIC KEY-----
        # MIGnMBAGByqGSM49AgEGBSuBBAAnA4GSAAQDgGlczbL2TW/CHwcM8VHzvT4Vlgmm
        # QqqgI7NiDT9FKk1hFKkDHB8RVf3Gw9iWicAuxyBeMGoeo5fVmo4AVnAtcE2X1opw
        # qbAA4rkC0v+5IQcSXSTZHLdx8RzqiLFXyNZCHq7MHdudba1KkHNz8Is/HrEkONKQ
        # 8oL+Klzsny3rcbI6d+dKeHyvn68qICrb9yg=
        #-----END PUBLIC KEY-----

        master_key = "3081a7301006072a8648ce3d020106052b8104002703819200040380695ccdb2f64d6fc21f070cf151f3bd3e159609a642aaa023b3620d3f452a4d6114a9031c1f1155fdc6c3d89689c02ec7205e306a1ea397d59a8e0056702d704d97d68a70a9b000e2b902d2ffb92107125d24d91cb771f11cea88b157c8d6421eaecc1ddb9d6dad4a907373f08b3f1eb12438d290f282fe2a5cec9f2deb71b23a77e74a787caf9faf2a202adbf728".decode("HEX")
        master = dispersy.get_member(public_key=master_key)
        return [master]

    def initiate_meta_messages(self):
        return super(TunnelCommunity, self).initiate_meta_messages() + \
               [Message(self, u"cell", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), CellPayload(), self._generic_timeline_check, self.on_cell),
                Message(self, u"create", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), CreatePayload(), self._generic_timeline_check, self.on_create),
                Message(self, u"created", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), CreatedPayload(), self.check_created, self.on_created),
                Message(self, u"extend", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), ExtendPayload(), self.check_extend, self.on_extend),
                Message(self, u"extended", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), ExtendedPayload(), self.check_extended, self.on_extended),
                Message(self, u"ping", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), PingPayload(), self._generic_timeline_check, self.on_ping),
                Message(self, u"pong", NoAuthentication(), PublicResolution(), DirectDistribution(), CandidateDestination(), PongPayload(), self.check_pong, self.on_pong)]

    def initiate_conversions(self):
        return [DefaultConversion(self), TunnelConversion(self)]

    def unload_community(self):
        self.socks_server.stop()
        for exit_socket in self.exit_sockets.itervalues():
            if exit_socket:
                exit_socket.close()

        super(TunnelCommunity, self).unload_community()

    @property
    def crypto(self):
        return self.settings.crypto

    @property
    def dispersy_enable_bloom_filter_sync(self):
        return False

    @property
    def dispersy_enable_fast_candidate_walker(self):
        return True

    def _generate_circuit_id(self, neighbour=None):
        circuit_id = random.getrandbits(32)

        # Prevent collisions.
        while circuit_id in self.circuits or (neighbour and (neighbour, circuit_id) in self.relay_from_to):
            circuit_id = random.getrandbits(32)

        return circuit_id

    def do_circuits(self):
        circuit_needed = self.settings.max_circuits - len(self.circuits)
        logger.debug("TunnelCommunity: want %d circuits", circuit_needed)

        for _ in range(circuit_needed):
            candidate = None
            hops = set([c.first_hop for c in self.circuits.values()])
            for c in self.dispersy_yield_verified_candidates():
                if (c.sock_addr not in hops) and self.crypto.is_key_compatible(c.get_member()._ec):
                    candidate = c
                    break

            if candidate != None:
                try:
                    self.create_circuit(candidate, self.settings.circuit_length)
                except:
                    logger.exception("Error creating circuit while running __discover")

        if self.tribler_session and not self.made_anon_session and len(self.active_circuits) >= self.settings.min_circuits_for_session:
            try:
                ltmgr = self.tribler_session.get_libtorrent_process()
                ltmgr.create_anonymous_session()
            except OperationNotEnabledByConfigurationException:
                pass
            self.made_anon_session = True

        self.do_break()

    def do_break(self):
        # Remove circuits that are inactive / are too old / have transferred too many bytes.
        for key, circuit in self.circuits.items():
            if circuit.last_incoming < time.time() - self.settings.max_time_inactive:
                self.remove_circuit(key, 'no activity')
            elif circuit.creation_time < time.time() - self.settings.max_time:
                self.remove_circuit(key, 'too old')
            elif circuit.bytes_up + circuit.bytes_down > self.settings.max_traffic:
                self.remove_circuit(key, 'traffic limit exceeded')

        # Remove relays that are inactive / are too old / have transferred too many bytes.
        for key, relay in self.relay_from_to.items():
            if relay.last_incoming < time.time() - self.settings.max_time_inactive:
                self.remove_relay(key, 'no activity')
            elif relay.creation_time < time.time() - self.settings.max_time:
                self.remove_relay(key, 'too old')
            elif relay.bytes_relayed > self.settings.max_traffic:
                self.remove_relay(key, 'traffic limit exceeded')

        # Remove exit sockets that are too old / have transferred too many bytes.
        for circuit_id, exit_socket in self.exit_sockets.items():
            if exit_socket and exit_socket.creation_time < time.time() - self.settings.max_time:
                self.remove_exit_socket(circuit_id, 'too old')
            elif exit_socket and exit_socket.bytes_up + exit_socket.bytes_down > self.settings.max_traffic:
                self.remove_exit_socket(circuit_id, 'traffic limit exceeded')

    def create_circuit(self, first_hop, goal_hops):
        circuit_id = self._generate_circuit_id(first_hop.sock_addr)
        circuit = Circuit(circuit_id=circuit_id, goal_hops=goal_hops, first_hop=first_hop.sock_addr, proxy=self)

        self.request_cache.add(CircuitRequestCache(self, circuit))

        circuit.unverified_hop = Hop(first_hop.get_member()._ec)
        circuit.unverified_hop.address = first_hop.sock_addr
        circuit.unverified_hop.dh_secret, circuit.unverified_hop.dh_first_part = self.crypto.generate_diffie_secret()

        logger.info("TunnelCommunity: creating circuit %d of %d hops. First hop: %s:%d", circuit_id,
                       circuit.goal_hops, first_hop.sock_addr[0], first_hop.sock_addr[1])

        self.circuits[circuit_id] = circuit
        self.waiting_for.add(circuit_id)

        dh_first_part_enc = self.crypto.hybrid_encrypt_str(first_hop.get_member()._ec, long_to_bytes(circuit.unverified_hop.dh_first_part))
        circuit.bytes_up += self.send_cell([first_hop], u"create", (circuit_id, dh_first_part_enc))
        return circuit

    def remove_circuit(self, circuit_id, additional_info=''):
        assert isinstance(circuit_id, (long, int)), type(circuit_id)

        if circuit_id in self.circuits:
            logger.error("TunnelCommunity: breaking circuit %d " + additional_info, circuit_id)

            circuit = self.circuits.pop(circuit_id)
            circuit.destroy()

            affected_peers = self.socks_server.circuit_dead(circuit)

#         affected_torrents = dict((download, affected_destinations.intersection(peer.ip for peer in download.handle.get_peer_info()))
#                                  for (download, session) in mgr.torrents.values() if session == anon_session)
#
#         for download, peers in affected_torrents:
#             if download not in self.torrents:
#                 self.torrents[download] = peers
#             elif peers - self.torrents[download]:
#                 self.torrents[download] = peers | self.torrents[download]
#
#         self._logger.warning("Waiting for new circuits before re-adding peers")

            return True
        return False

    def remove_relay(self, circuit_id, additional_info=''):
        if circuit_id in self.relay_from_to:
            logger.error(("TunnelCommunity: breaking relay %d " + additional_info) % circuit_id)
            # Only remove one side of the relay, this isn't as pretty but both sides have separate incoming timer,
            # hence after removing one side the other will follow.
            del self.relay_from_to[circuit_id]
            return

        logger.error(("TunnelCommunity: could not break relay %d " + additional_info) % circuit_id)

    def remove_exit_socket(self, circuit_id, additional_info=''):
        if circuit_id in self.exit_sockets:
            exit_socket = self.exit_sockets.pop(circuit_id)
            if exit_socket:
                logger.error(("TunnelCommunity: removed exit socket %d " + additional_info) % circuit_id)
                exit_socket.close()
            return
        logger.error(("TunnelCommunity: could not remove exit socket %d " + additional_info) % circuit_id)

    @property
    def active_circuits(self):
        return {cid: c for cid, c in self.circuits.iteritems() if c.state == CIRCUIT_STATE_READY}

    def is_relay(self, circuit_id):
        return circuit_id > 0 and circuit_id in self.relay_from_to and not circuit_id in self.waiting_for

    def send_cell(self, candidates, message_type, payload):
        meta = self.get_meta_message(message_type)
        message = meta.impl(distribution=(self.global_time,), payload=payload)
        packet = TunnelConversion.convert_to_cell(message.packet)

        plaintext, encrypted = TunnelConversion.split_encrypted_packet(packet, u'cell')
        if message_type not in [u'create', u'created']:
            try:
                encrypted = self.crypto_out(message.payload.circuit_id, encrypted)
            except CryptoException, e:
                logger.error(str(e))
                return 0
        packet = plaintext + encrypted

        return self.send_packet(candidates, message_type, packet)

    def send_data(self, candidates, message_type, packet):
        circuit_id, _, _, _ = TunnelConversion.decode_data(packet)

        plaintext, encrypted = TunnelConversion.split_encrypted_packet(packet, message_type)
        try:
            encrypted = self.crypto_out(circuit_id, encrypted)
        except CryptoException, e:
            logger.error(str(e))
            return 0
        packet = plaintext + encrypted

        return self.send_packet(candidates, u'data', packet)

    def send_packet(self, candidates, message_type, packet):
        self.dispersy._endpoint.send(candidates, [packet], prefix=self.data_prefix if message_type == u"data" else None)
        self.statistics.increase_msg_count(u"outgoing", message_type, len(candidates))
        logger.debug("TunnelCommunity: send %s to %s candidates: %s", message_type, len(candidates), map(str, candidates))
        return len(packet)

    def relay_cell(self, circuit_id, message_type, message):
        return self.relay_packet(circuit_id, message_type, message.packet)

    def relay_packet(self, circuit_id, message_type, packet):
        if self.is_relay(circuit_id):
            next_relay = self.relay_from_to[circuit_id]
            this_relay = self.relay_from_to.get(next_relay.circuit_id, None)

            if this_relay:
                this_relay.last_incoming = time.time()

            plaintext, encrypted = TunnelConversion.split_encrypted_packet(packet, message_type)
            try:
                encrypted = self.crypto_relay(circuit_id, encrypted)
            except CryptoException, e:
                logger.error(str(e))
                return False
            packet = plaintext + encrypted

            packet = TunnelConversion.swap_circuit_id(packet, message_type, circuit_id, next_relay.circuit_id)
            bytes_relayed = self.send_packet([Candidate(next_relay.sock_addr, False)], message_type, packet)

            if this_relay:
                this_relay.bytes_relayed = bytes_relayed

            return True
        return False

    def check_extend(self, messages):
        for message in messages:
            if not self.is_relay(message.payload.circuit_id):
                request = self.request_cache.get(u"anon-created", message.payload.circuit_id)
                if not request:
                    yield DropMessage(message, "invalid extend request circuit_id")
                    continue
            yield message

    def check_created(self, messages):
        for message in messages:
            if not self.is_relay(message.payload.circuit_id) and message.payload.circuit_id in self.circuits:
                request = self.request_cache.get(u"anon-circuit", message.payload.circuit_id)
                if not request:
                    yield DropMessage(message, "invalid created response circuit_id")
                    continue
            yield message

    def check_extended(self, messages):
        for message in messages:
            if not self.is_relay(message.payload.circuit_id):
                request = self.request_cache.get(u"anon-circuit", message.payload.circuit_id)
                if not request:
                    yield DropMessage(message, "invalid extended response circuit_id")
                    continue
            yield message

    def check_pong(self, messages):
        for message in messages:
            if not self.is_relay(message.payload.circuit_id):
                request = self.request_cache.get(u"ping", message.payload.identifier)
                if not request:
                    yield DropMessage(message, "invalid ping identifier")
                    continue
            yield message

    def _ours_on_created_extended(self, circuit, message):
        hop = circuit.unverified_hop
        hop.session_keys = self.crypto.generate_session_keys(hop.dh_secret, bytes_to_long(message.payload.key))

        circuit.add_hop(hop)
        circuit.unverified_hop = None

        if circuit.state == CIRCUIT_STATE_EXTENDING:
            candidate_list_enc = message.payload.candidate_list
            _, candidate_list = decode(self.crypto.decrypt_str(hop.session_keys[ENDPOINT], candidate_list_enc))

            for ignore_candidate in [self.my_member.public_key] + [hop.public_key for hop in circuit.hops]:
                if ignore_candidate in candidate_list:
                    candidate_list.remove(ignore_candidate)

            for i in range(len(candidate_list) - 1, -1, -1):
                public_key = self.crypto.key_from_public_bin(candidate_list[i])
                if not self.crypto.is_key_compatible(public_key):
                    candidate_list.pop(i)

            extend_hop_public_bin = next(iter(candidate_list), None)
            if extend_hop_public_bin:
                extend_hop_public_key = self.dispersy.crypto.key_from_public_bin(extend_hop_public_bin)
                hashed_public_key = self.dispersy.crypto.key_to_hash(extend_hop_public_key)
                circuit.unverified_hop = Hop(extend_hop_public_key)
                circuit.unverified_hop.dh_secret, circuit.unverified_hop.dh_first_part = self.crypto.generate_diffie_secret()

                logger.info("TunnelCommunity: extending circuit %d with %s", circuit.circuit_id, hashed_public_key)
                dh_first_part_enc = self.crypto.hybrid_encrypt_str(extend_hop_public_key, long_to_bytes(circuit.unverified_hop.dh_first_part))
                circuit.bytes_up += self.send_cell([Candidate(circuit.first_hop, False)], u"extend", \
                                                   (circuit.circuit_id, dh_first_part_enc, extend_hop_public_bin))
            else:
                self.remove_circuit(circuit.circuit_id, "no candidates to extend, bailing out.")

        elif circuit.state == CIRCUIT_STATE_READY:
            self.request_cache.pop(u"anon-circuit", circuit.circuit_id)
        else:
            return

        if self.notifier:
            from Tribler.Core.simpledefs import NTFY_ANONTUNNEL, NTFY_CREATED, NTFY_EXTENDED
            self.notifier.notify(NTFY_ANONTUNNEL, NTFY_CREATED if len(circuit.hops) == 1 else NTFY_EXTENDED, circuit)

    def on_cell(self, messages):
        decrypted_packets = []

        for message in messages:
            circuit_id = message.payload.circuit_id
            logger.debug("TunnelCommunity: got %s (%d) from %s", message.payload.message_type, message.payload.circuit_id, message.candidate.sock_addr)
            # TODO: if crypto fails for relay messages, call remove_relay
            # TODO: if crypto fails for other messages, call remove_circuit
            if not self.relay_cell(circuit_id, message.payload.message_type, message):

                plaintext, encrypted = TunnelConversion.split_encrypted_packet(message.packet, message.name)
                if message.payload.message_type not in [u'create', u'created']:
                    try:
                        encrypted = self.crypto_in(circuit_id, encrypted)
                    except CryptoException, e:
                        logger.error(str(e))
                        continue

                packet = plaintext + encrypted

                decrypted_packets.append((message.candidate, TunnelConversion.convert_from_cell(packet)))

                if circuit_id in self.circuits:
                    self.circuits[circuit_id].beat_heart()
                    self.circuits[circuit_id].bytes_down += len(message.packet)

        if decrypted_packets:
            self._dispersy.on_incoming_packets(decrypted_packets, cache=False)

    def on_create(self, messages):
        for message in messages:
            candidate = message.candidate
            circuit_id = message.payload.circuit_id

            if self.settings.max_relays_or_exits <= len(self.relay_from_to) + len(self.exit_sockets):
                logger.error('TunnelCommunity: ignoring create for circuit %d from %s (too many relays %d/%d)', circuit_id, candidate.sock_addr, len(self.relay_from_to) + len(self.exit_sockets), self.settings.max_relays_or_exits)
                continue

            try:
                dh_second_part = self.crypto.hybrid_decrypt_str(self.my_member._ec, message.payload.key)
            except CryptoException, e:
                logger.error(str(e))
                continue

            self.directions[circuit_id] = ENDPOINT
            logger.info('TunnelCommunity: we joined circuit %d with neighbour %s', circuit_id, candidate.sock_addr)
            dh_secret, dh_first_part = self.crypto.generate_diffie_secret()

            self.relay_session_keys[circuit_id] = self.crypto.generate_session_keys(dh_secret, bytes_to_long(dh_second_part))

            candidates = {}
            for c in self.dispersy_yield_verified_candidates():
                candidates[c.get_member().public_key] = c
                if len(candidates) >= 4:
                    break

            self.request_cache.add(CreatedRequestCache(self, circuit_id, candidate, candidates))

            self.exit_sockets[circuit_id] = None

            if self.notifier:
                from Tribler.Core.simpledefs import NTFY_ANONTUNNEL, NTFY_JOINED
                self.notifier.notify(NTFY_ANONTUNNEL, NTFY_JOINED, candidate.sock_addr, circuit_id)

            candidate_list_enc = self.crypto.encrypt_str(self.relay_session_keys[circuit_id][ENDPOINT], encode(candidates.keys()))
            self.send_cell([candidate], u"created", (circuit_id, long_to_bytes(dh_first_part), candidate_list_enc))

    def on_created(self, messages):
        for message in messages:
            candidate = message.candidate
            circuit_id = message.payload.circuit_id

            if circuit_id not in self.waiting_for:
                logger.error("TunnelCommunity: got an unexpected CREATED message for circuit %d from %s:%d", circuit_id, *candidate.sock_addr)
                continue
            self.waiting_for.remove(circuit_id)

            self.directions[circuit_id] = ORIGINATOR
            if circuit_id in self.relay_from_to:
                logger.debug("TunnelCommunity: got CREATED message forward as EXTENDED to origin.")

                forwarding_relay = self.relay_from_to[circuit_id]
                self.send_cell([Candidate(forwarding_relay.sock_addr, False)], u"extended", \
                               (forwarding_relay.circuit_id, message.payload.key, message.payload.candidate_list))

            # Circuit is ours.
            if circuit_id in self.circuits:
                circuit = self.circuits[circuit_id]
                self._ours_on_created_extended(circuit, message)

    def on_extend(self, messages):
        for message in messages:
            if message.payload.extend_with:
                candidate = message.candidate
                circuit_id = message.payload.circuit_id
                request = self.request_cache.pop(u"anon-created", circuit_id)

                extend_candidate = request.candidates[message.payload.extend_with]
                logger.info("TunnelCommunity: on_extend send CREATE for circuit (%s, %d) to %s:%d!", candidate.sock_addr,
                                circuit_id, extend_candidate.sock_addr[0], extend_candidate.sock_addr[1])
            else:
                logger.error("TunnelCommunity: cancelling EXTEND, no candidate!")
                continue

            if circuit_id in self.relay_from_to:
                current_relay = self.relay_from_to.pop(circuit_id)
                assert not current_relay.online, "shouldn't be called whenever relay is online the extend message should have been forwarded"

                # We will just forget the attempt and try again, possibly with another candidate.
                del self.relay_from_to[current_relay.circuit_id]

            new_circuit_id = self._generate_circuit_id(extend_candidate.sock_addr)

            self.waiting_for.add(new_circuit_id)
            self.relay_from_to[new_circuit_id] = RelayRoute(circuit_id, candidate.sock_addr)
            self.relay_from_to[circuit_id] = RelayRoute(new_circuit_id, extend_candidate.sock_addr)

            self.relay_session_keys[new_circuit_id] = self.relay_session_keys[circuit_id]

            self.directions[new_circuit_id] = ORIGINATOR
            self.directions[circuit_id] = ENDPOINT

            if circuit_id in self.exit_sockets:
                self.remove_exit_socket(circuit_id)

            logger.info("TunnelCommunity: extending circuit, got candidate with IP %s:%d from cache", *extend_candidate.sock_addr)

            self.send_cell([extend_candidate], u"create", (new_circuit_id, message.payload.key))

    def on_extended(self, messages):
        for message in messages:
            circuit_id = message.payload.circuit_id
            circuit = self.circuits[circuit_id]
            self._ours_on_created_extended(circuit, message)

    @call_on_reactor_thread
    def on_data(self, sock_addr, packet):
        # If its our circuit, the messenger is the candidate assigned to that circuit and the DATA's destination
        # is set to the zero-address then the packet is from the outside world and addressed to us from.

        message_type = u'data'
        circuit_id = TunnelConversion.get_circuit_id(packet, message_type)

        logger.debug("TunnelCommunity: got data (%d) from %s", circuit_id, sock_addr)

        if not self.relay_packet(circuit_id, message_type, packet):
            plaintext, encrypted = TunnelConversion.split_encrypted_packet(packet, message_type)

            try:
                encrypted = self.crypto_in(circuit_id, encrypted)
            except CryptoException, e:
                logger.error(str(e))
                return

            packet = plaintext + encrypted
            circuit_id, destination, origin, data = TunnelConversion.decode_data(packet)

            if circuit_id in self.circuits and origin and sock_addr == self.circuits[circuit_id].first_hop:
                self.circuits[circuit_id].beat_heart()
                self.circuits[circuit_id].bytes_down += len(packet)

                self.socks_server.on_incoming_from_tunnel(self, self.circuits[circuit_id], origin, data)

            # It is not our circuit so we got it from a relay, we need to EXIT it!
            else:
                logger.debug("TunnelCommunity: data for circuit %d exiting tunnel (%s)", circuit_id, destination)
                if destination != ('0.0.0.0', 0):
                    self.exit_data(circuit_id, sock_addr, destination, data)
                else:
                    logger.error("cannot exit data, destination is 0.0.0.0:0")

    def on_ping(self, messages):
        for message in messages:
            if message.payload.circuit_id in self.exit_sockets:
                self.send_cell([message.candidate], u"pong", (message.payload.circuit_id, message.payload.identifier))
                logger.debug("TunnelCommunity: got ping from %s", message.candidate)
            else:
                logger.error("TunnelCommunity: got ping from %s (not responding)", message.candidate)

    def on_pong(self, messages):
        for message in messages:
            self.request_cache.pop(u"ping", message.payload.identifier)
            logger.debug("TunnelCommunity: got pong from %s", message.candidate)

    def do_ping(self):
        # Ping circuits. Pings are only sent to the first hop, subsequent hops will relay the ping.
        for circuit in self.active_circuits.values():
            if circuit.goal_hops > 0:
                cache = self.request_cache.add(PingRequestCache(self, circuit))
                circuit.bytes_up += self.send_cell([Candidate(circuit.first_hop, False)], u"ping", (circuit.circuit_id, cache.number))

    def tunnel_data_to_end(self, ultimate_destination, data, circuit):
        packet = TunnelConversion.encode_data(circuit.circuit_id, ultimate_destination, ('0.0.0.0', 0), data)
        circuit.bytes_up += self.send_data([Candidate(circuit.first_hop, False)], u'data', packet)

    def tunnel_data_to_origin(self, circuit_id, sock_addr, source_address, data):
        packet = TunnelConversion.encode_data(circuit_id, ('0.0.0.0', 0), source_address, data)
        self.send_data([Candidate(sock_addr, False)], u'data', packet)

    def exit_data(self, circuit_id, sock_addr, destination, data):
        if circuit_id in self.exit_sockets:
            if not self.exit_sockets[circuit_id]:
                self.exit_sockets[circuit_id] = TunnelExitSocket(circuit_id, sock_addr, self)
            try:
                self.exit_sockets[circuit_id].sendto(data, destination)
            except:
                logger.error("TunnelCommunity: dropping data packets while EXITing")
                print_exc()
        else:
            logger.error("TunnelCommunity: dropping data packets with unknown circuit_id")

    def crypto_out(self, circuit_id, content):
        if circuit_id in self.circuits:
            for hop in reversed(self.circuits[circuit_id].hops):
                content = self.crypto.encrypt_str(hop.session_keys[ENDPOINT], content)
            return content
        elif circuit_id in self.relay_session_keys:
            return self.crypto.encrypt_str(self.relay_session_keys[circuit_id][ORIGINATOR], content)
        raise CryptoException("Don't know how to encrypt outgoing message for circuit_id %d" % circuit_id)

    def crypto_in(self, circuit_id, content):
        if circuit_id in self.circuits and len(self.circuits[circuit_id].hops) > 0:
            for hop in self.circuits[circuit_id].hops:
                content = self.crypto.decrypt_str(hop.session_keys[ORIGINATOR], content)
            return content
        elif circuit_id in self.relay_session_keys:
            return self.crypto.decrypt_str(self.relay_session_keys[circuit_id][ENDPOINT], content)
        raise CryptoException("Don't know how to decrypt incoming message for circuit_id %d" % circuit_id)

    def crypto_relay(self, circuit_id, content):
        direction = self.directions[circuit_id]
        if direction == ORIGINATOR:
            return self.crypto.encrypt_str(self.relay_session_keys[circuit_id][direction], content)
        elif direction == ENDPOINT:
            return self.crypto.decrypt_str(self.relay_session_keys[circuit_id][direction], content)
        raise CryptoException("Direction must be either ORIGINATOR or ENDPOINT")
