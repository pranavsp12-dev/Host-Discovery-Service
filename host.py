from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types
from ryu.lib import hub
import time


class HostDiscovery(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HostDiscovery, self).__init__(*args, **kwargs)
        self.host_db = {}
        self.datapaths = {}
        self.mac_to_port = {}
        self.monitor_thread = hub.spawn(self.monitor_hosts)

    # ✅ NEW: text file logger
    def write_log(self, event, mac, ip, dpid, port):
        with open("host_history.txt", "a") as f:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(
                f"[{timestamp}] {event} | "
                f"MAC: {mac} | "
                f"IP: {ip} | "
                f"Switch: {dpid} | "
                f"Port: {port}\n"
            )

    def print_host_db(self):
        print("\n===== CURRENT HOST DATABASE =====")
        for mac, info in self.host_db.items():
            print(
                f"MAC: {mac} | "
                f"IP: {info['ip']} | "
                f"Switch: {info['switch']} | "
                f"Port: {info['port']} | "
                f"Status: {info['status']}"
            )
        print("================================")

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        inst = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']

        self.mac_to_port.setdefault(dpid, {})

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src

        self.mac_to_port[dpid][src] = in_port

        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        src_ip = None
        if arp_pkt:
            src_ip = arp_pkt.src_ip
        elif ip_pkt:
            src_ip = ip_pkt.src

        if src not in self.host_db and src_ip:
            self.host_db[src] = {
                "ip": src_ip,
                "switch": dpid,
                "port": in_port,
                "status": "ACTIVE",
                "last_seen": time.time()
            }
            print(f"\nNEW HOST JOINED: {src}")
            self.write_log("HOST_JOINED", src, src_ip, dpid, in_port)
            self.print_host_db()

        elif src in self.host_db:
            if self.host_db[src]["status"] == "LEFT":
                print(f"\nHOST REJOINED: {src}")
                self.write_log("HOST_REJOINED", src, src_ip, dpid, in_port)

            self.host_db[src]["status"] = "ACTIVE"
            self.host_db[src]["port"] = in_port
            self.host_db[src]["last_seen"] = time.time()

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=msg.data
        )
        datapath.send_msg(out)

    def monitor_hosts(self):
        while True:
            current_time = time.time()

            for mac in list(self.host_db.keys()):
                host = self.host_db[mac]

                if host["status"] == "ACTIVE":
                    if current_time - host["last_seen"] > 15:
                        host["status"] = "LEFT"
                        print(f"\nHOST LEFT: {mac}")
                        self.write_log(
                            "HOST_LEFT",
                            mac,
                            host["ip"],
                            host["switch"],
                            host["port"]
                        )
                        self.print_host_db()

            hub.sleep(5)
