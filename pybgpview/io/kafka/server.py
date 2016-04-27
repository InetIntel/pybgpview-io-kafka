import argparse
import logging
import pykafka
import struct
import time

METADATA_TOPIC = "meta"
GLOBAL_METADATA_TOPIC = "globalmeta"
MEMBERS_TOPIC = "members"

# Once the first member publishes a view for time X, we will wait for 30 mins
# for all members to have published a view before we will call it a global view
# and publish it anyway
PUBLICATION_TIMEOUT_DEFAULT = 1800

# We will allow at most 2 hours to go by between updates to the members topic
# before a member is declared dead.
MEMBER_TIMEOUT_DEFAULT = 3600*2

METRIC_PREFIX_DEFAULT = "bgp"
METRIC_PATH = "meta.bgpview.server.kafka"


class Server:
    """ Watches the members and metadata topics of a BGPView Kafka stream and
    signals availability of global views
    """

    def __init__(self,
                 brokers,
                 namespace="bgpview-test",
                 publication_timeout=PUBLICATION_TIMEOUT_DEFAULT,
                 member_timeout=MEMBER_TIMEOUT_DEFAULT,
                 metric_prefix=METRIC_PREFIX_DEFAULT):
        self.brokers = brokers
        self.namespace = namespace
        self.pub_timeout = publication_timeout
        self.member_timeout = member_timeout
        self.metric_prefix = metric_prefix
        self.last_pub_time = 0
        self.last_sync_offset = -1

        # our active members
        self.members = dict()
        # our partial views
        self.views = dict()

        # configure the logger
        logging.basicConfig(level='INFO',
                            format='%(asctime)s|SERVER|%(levelname)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

        # connect to kafka
        self.kc = pykafka.KafkaClient(hosts=self.brokers)
        # set up our consumers
        self.md_consumer =\
            self.topic(METADATA_TOPIC).get_simple_consumer(consumer_timeout_ms=10000)
        self.members_consumer =\
            self.topic(MEMBERS_TOPIC).get_simple_consumer(consumer_timeout_ms=1000)
        self.gmd_consumer =\
            self.topic(GLOBAL_METADATA_TOPIC).get_simple_consumer(consumer_timeout_ms=1000)
        # and our producer
        self.gmd_producer =\
            self.topic(GLOBAL_METADATA_TOPIC).get_sync_producer()

    def topic(self, name):
        return self.kc.topics[self.namespace + '.' + name]

    def dump_metric(self, metric, value, time):
        print "%s.%s.%s %d %d" %\
              (self.metric_prefix, METRIC_PATH, metric, value, time)

    def update_members(self):
        logging.info("Starting member update with %d members" %
                     len(self.members))

        # read all the messages in the members topic and build a list of
        # active members along with their last-seen times
        for msg in self.members_consumer:
            if msg is not None:
                parsed = self.parse_member_msg(msg.value)
                if parsed['time']:
                    self.members[parsed['collector']] = parsed['time']
                elif parsed['collector'] in self.members:
                    del self.members[parsed['collector']]

        # now go through and evict any members that have a last-seen time older
        # than our timeout
        time_now = int(time.time())
        for member in self.members.keys():
            if self.members[member] < (time_now - self.member_timeout):
                logging.warn("Removing dead member %s. Last seen at %d" %
                             (member, self.members[member]))
                del self.members[member]

        logging.info("Finished member update. We now have %d members." %
                     len(self.members))

    def scan_global_metadata(self):
        for msg in self.gmd_consumer:
            self.handle_gmd_msg(msg)
        logging.info("Finished global metadata scan. Last published time: %d" %
                     self.last_pub_time)

    def load_metadata(self):
        for msg in self.md_consumer:
            self.handle_md_msg(msg)
        self.handle_timeouts()

    def maybe_publish_view(self, view_time):
        if view_time <= self.last_pub_time:
            # already published a view for this time, ignore this view
            logging.info("Skipping view for %d" % view_time)
            return

        time_now = int(time.time())
        contributors_cnt = len(self.views[view_time]['members'])
        tv = self.views[view_time]
        if tv['arr_time'] + self.pub_timeout <= time_now or \
                contributors_cnt == len(self.members):
            logging.info("Publishing view for %d at %d "
                         "(%ds realtime delay, %ds buffer delay) "
                         "with %d members" %
                         (view_time, time_now, time_now - view_time,
                          time_now - tv['arr_time'],
                          contributors_cnt))
            self.dump_metric("publication.realtime_delay",
                             time_now - view_time, view_time)
            self.dump_metric("publication.buffer_delay",
                             time_now - tv['arr_time'], view_time)
            self.dump_metric("publication.member_cnt",
                             contributors_cnt, view_time)
            # TODO: add count of peers
            self.dump_metric("member_cnt", len(self.members), view_time)
            if contributors_cnt < len(self.members):
                # find which member(s) didn't contribute
                missing = [m for m in self.members
                           if m not in tv['collectors']]
                logging.info("Published view at %d was missing data from: %s" %
                             (view_time, missing))
            if tv['type'] == 'S':
                self.last_sync_offset = -1
            self.send_gmd_msg(view_time)
            del self.views[view_time]
            self.last_pub_time = view_time

    def handle_timeouts(self):
        for view_time in sorted(self.views.keys()):
            self.maybe_publish_view(view_time)

    def handle_gmd_msg(self, msg):
        msg = self.parse_gmd_msg(msg.value)
        if msg['time'] > self.last_pub_time:
            self.last_pub_time = msg['time']

    def handle_md_msg(self, msg):
        msg = self.parse_md_msg(msg.value)
        view_time = msg['time']

        if view_time <= self.last_pub_time:
            # already published a view for this time, ignore this message
            logging.info("Skipping view for %d" % view_time)
            return None

        if view_time not in self.views:
            time_now = int(time.time())
            nv = dict()
            nv['arr_time'] = time_now
            nv['type'] = msg['type']
            nv['members'] = []
            nv['collectors'] = []
            self.views[view_time] = nv

        self.views[view_time]['members'].append(msg)
        self.views[view_time]['collectors'].append(msg['collector'])

        return view_time

    def send_gmd_msg(self, view_time):
        tv = self.views[view_time]
        if tv['type'] == 'S':
            self.last_sync_offset = -1
        logging.info("Setting last sync offset: %d" % self.last_sync_offset)
        msg = self.serialize_gmd_msg(view_time,
                                     self.last_sync_offset,
                                     tv['members'])
        self.gmd_producer.produce(msg)
        next_offset = self.topic(GLOBAL_METADATA_TOPIC).\
            latest_available_offsets()[0][0][0]
        if tv['type'] == 'S':
            self.last_sync_offset = next_offset - 1

    def log_state(self):
        logging.info("Currently tracking %d partial views:" % len(self.views))
        for view_time in sorted(self.views):
            logging.info("  Time: %d, # Members: %d" %
                         (view_time, len(self.views[view_time]['members'])))

    def run(self):
        # first, build our current membership
        self.update_members()

        # second, lets see what already exists in the global meta topic
        self.scan_global_metadata()

        # now, read the entire metadata topic
        self.load_metadata()

        # now, loop forever reading metadata
        while True:
            for msg in self.md_consumer:
                if msg is not None:
                    view_time = self.handle_md_msg(msg)
                    if view_time:
                        self.maybe_publish_view(view_time)
                    self.handle_timeouts()
                    self.update_members()
                    self.log_state()
            self.update_members()

    @staticmethod
    def parse_member_msg(msg):
        (strlen) = struct.unpack("=H", msg[0:2])
        (collector, time) = struct.unpack("=%dsL" % strlen, msg[2:])
        return {'collector': collector, 'time': time}

    @staticmethod
    def parse_gmd_msg(msg):
        # there is lots of info in there, but we just want the time
        view_time = struct.unpack("=L", msg[0:4])
        return {'time': view_time[0]}

    @staticmethod
    def parse_md_msg(msg):
        strlen = struct.unpack("=H", msg[0:2])
        msglen = strlen[0] + 4 + 8 + 8 + 1
        (collector, time, pfxs_offset, peers_offset, type) =\
            struct.unpack("=%dsLQQc" % strlen, msg[2:2+msglen])
        res = {
            'collector': collector,
            'time': time,
            'pfxs_offset': pfxs_offset,
            'peers_offset': peers_offset,
            'type': type,
        }
        if type == 'D':
            # there are an extra couple of fields in a diff message
            (sync_md_offset, parent_time) = struct.unpack("=QL", msg[2+msglen:])
            res['sync_md_offset'] = sync_md_offset
            res['parent_time'] = parent_time
        return res

    @staticmethod
    def serialize_gmd_msg(view_time, last_sync_offset, members):
        msg = struct.pack("=LH", view_time, len(members))
        parts = []
        for member in members:
            coll = member['collector']
            mmsg = struct.pack("=H", len(coll)) + \
                struct.pack("=%dsLQQc" % len(coll),
                            coll,
                            view_time,
                            member['pfxs_offset'],
                            member['peers_offset'],
                            member['type'])
            if member['type'] == 'D':
                mmsg += struct.pack("=QL",
                                    member['sync_md_offset'],
                                    member['parent_time'])
            parts.append(mmsg)
        parts.append(struct.pack("=q", last_sync_offset))
        return msg + ''.join(parts)


def main():
    parser = argparse.ArgumentParser(description="""
    Watches the members and metadata topics of a BGPView Kafka stream and
    signals availability of global views
    """)
    parser.add_argument('-b',  '--brokers',
                        nargs='?', required=True,
                        help='Comma-separated list of broker URIs')
    parser.add_argument('-n',  '--namespace',
                        nargs='?', required=False,
                        default='bgpview-test',
                        help='BGPView Kafka namespace to use')
    parser.add_argument('-t',  '--publication-timeout',
                        nargs='?', required=False,
                        type=int,
                        default=PUBLICATION_TIMEOUT_DEFAULT,
                        help='Publication timeout: How long to wait for' +
                        ' all members to have contributed a view.')
    parser.add_argument('-m',  '--member-timeout',
                        nargs='?', required=False,
                        type=int,
                        default=MEMBER_TIMEOUT_DEFAULT,
                        help='Member timeout: How much time may elapse between'
                        + ' messages to the members topic before a member is'
                        + ' declared dead.')
    parser.add_argument('-p', '--metric-prefix',
                        nargs='?', required=False,
                        default=METRIC_PREFIX_DEFAULT,
                        help='Prefix to use for timeseries paths')

    opts = vars(parser.parse_args())

    server = Server(**opts)
    server.run()
