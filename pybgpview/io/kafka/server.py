#
# Copyright (C) 2014 The Regents of the University of California.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import argparse
import logging
import pykafka
import _pytimeseries
import struct
import sys
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
METRIC_PATH = "meta.bgpview.server.kafka.channels"

CONSUMER_GROUP_DEFAULT = "bgpview-server"

class Server:
    """ Watches the members and metadata topics of a BGPView Kafka stream and
    signals availability of global views
    """

    def __init__(self,
                 brokers,
                 timeseries_config,
                 namespace="bgpview-test",
                 pub_channel=None,
                 publication_timeout=PUBLICATION_TIMEOUT_DEFAULT,
                 member_timeout=MEMBER_TIMEOUT_DEFAULT,
                 metric_prefix=METRIC_PREFIX_DEFAULT,
                 consumer_group=CONSUMER_GROUP_DEFAULT):
        self.brokers = brokers
        self.namespace = namespace
        self.pub_channel = pub_channel
        self.pub_timeout = publication_timeout
        self.member_timeout = member_timeout
        self.metric_prefix = metric_prefix
        self.last_pub_time = 0
        self.last_sync_offset = -1
        self.consumer_group = consumer_group

        # our active members
        self.members = dict()
        # our partial views
        self.views = dict()

        # configure the logger
        logging.basicConfig(level='INFO',
                            format='%(asctime)s|SERVER|%(levelname)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

        self.ts = None
        self.kp = None
        self._init_timeseries(timeseries_config)

        # build the GMD topic
        self.gmd_topic = GLOBAL_METADATA_TOPIC
        if self.pub_channel:
            self.gmd_topic = GLOBAL_METADATA_TOPIC + "." + str(self.pub_channel)
        logging.info("Setting GMD topic to %s" % self.gmd_topic)

        # connect to kafka
        self.kc = pykafka.KafkaClient(hosts=self.brokers)
        # set up our consumers
        self.md_consumer =\
            self.topic(METADATA_TOPIC).get_simple_consumer(consumer_timeout_ms=10000, consumer_group=self.consumer_group)
        self.members_consumer =\
            self.topic(MEMBERS_TOPIC).get_simple_consumer(consumer_timeout_ms=1000, consumer_group=self.consumer_group)
        self.gmd_consumer =\
            self.topic(self.gmd_topic).get_simple_consumer(consumer_timeout_ms=1000, consumer_group=self.consumer_group)
        # and our producer
        self.gmd_producer =\
            self.topic(self.gmd_topic).get_sync_producer()

    def _init_timeseries(self, config):
        logging.info("Initializing PyTimeseries")
        self.ts = _pytimeseries.Timeseries()
        args = config.split(" ", 1)
        name = args[0]
        if len(args) == 2:
            opts = args[1]
        else:
            opts = None
        logging.info("Enabling timeseries backend '%s'" % name)
        be = self.ts.get_backend_by_name(name)
        if not be:
            logging.error("Could get get TS backend %s" % name)
            sys.exit(-1)
        if not self.ts.enable_backend(be, opts):
            logging.error("Could get enable TS backend %s" % name)
            sys.exit(-1)
        self.kp = self.ts.new_keypackage(reset=False, disable=True)

    def topic(self, name):
        return self.kc.topics[self.namespace + '.' + name]

    def update_metric(self, metric, value):
        path = METRIC_PATH + ".default"
        if self.pub_channel:
            path = METRIC_PATH + "." + str(self.pub_channel)
        key = "%s.%s.%s" % (self.metric_prefix, path, metric)
        key = key.encode()
        idx = self.kp.get_key(key)
        if idx is None:
            idx = self.kp.add_key(key)
        else:
            self.kp.enable_key(idx)
        self.kp.set(idx, value)

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
        for member in list(self.members.keys()):
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

    def maybe_publish_view(self, view_time, ignore_timeouts=False):
        if view_time <= self.last_pub_time:
            # already published a view for this time, ignore this view
            logging.info("Skipping view for %d" % view_time)
            return
        
        if view_time != sorted(self.views.keys())[0]:
            # earlier unpublished view exists
            logging.info("Delay publishing view for %d" % view_time)
            return

        time_now = int(time.time())
        tv = self.views[view_time]
        contributors_cnt = len(tv['members'])
        stime = view_time  # timeout based on realtime delay
        if (not ignore_timeouts and (stime + self.pub_timeout <= time_now)) or \
                contributors_cnt == len(self.members):
            logging.info("Publishing view for %d at %d "
                         "(%ds realtime delay, %ds buffer delay) "
                         "with %d members" %
                         (view_time, time_now, time_now - view_time,
                          time_now - tv['arr_time'],
                          contributors_cnt))
            self.update_metric("publication.realtime_delay",
                             time_now - view_time)
            self.update_metric("publication.buffer_delay",
                             time_now - tv['arr_time'])
            self.update_metric("publication.member_cnt",
                             contributors_cnt)
            self.update_metric("publication.peers_cnt", tv['peers_cnt'])
            self.update_metric("member_cnt", len(self.members))
            self.kp.flush(view_time)  # will also disable all keys
            if contributors_cnt < len(self.members):
                # find which member(s) didn't contribute
                missing = [m for m in self.members
                           if m not in tv['collectors']]
                logging.info("Published view at %d was missing data from: %s" %
                             (view_time, missing))
            if tv['type'] == b'S':
                self.last_sync_offset = -1
            self.send_gmd_msg(view_time)
            del self.views[view_time]
            self.last_pub_time = view_time

    def handle_timeouts(self):
        self.update_members()
        for view_time in sorted(self.views.keys()):
            # if this view is earlier than something we've already
            # published, stop tracking it
            if view_time <= self.last_pub_time:
                del self.views[view_time]
            else:
                self.maybe_publish_view(view_time)
        self.log_state()

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
            is_hist = True if time_now > view_time else False
            nv = dict()
            nv['arr_time'] = time_now
            nv['type'] = msg['type']
            nv['members'] = []
            nv['collectors'] = []
            nv['peers_cnt'] = 0
            nv['is_hist'] = is_hist
            self.views[view_time] = nv

        # only append the view if there is not already one from this collector
        if msg['collector'] not in self.views[view_time]['collectors']:
            self.views[view_time]['members'].append(msg)
            self.views[view_time]['collectors'].append(msg['collector'])
            self.views[view_time]['peers_cnt'] += int(msg['peers_cnt'])

        return view_time

    def send_gmd_msg(self, view_time):
        tv = self.views[view_time]
        if tv['type'] == b'S':
            self.last_sync_offset = -1
        logging.info("Setting last sync offset: %d" % self.last_sync_offset)
        msg = self.serialize_gmd_msg(view_time,
                                     self.last_sync_offset,
                                     tv['members'])
        self.gmd_producer.produce(msg)
        next_offset = self.topic(self.gmd_topic).\
            latest_available_offsets()[0][0][0]
        if tv['type'] == b'S':
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
                        self.maybe_publish_view(view_time, ignore_timeouts=True)
                self.handle_timeouts()
            self.handle_timeouts()

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
        msglen = strlen[0] + 4 + 4 + 8 + 8 + 1
        (collector, time, peers_cnt, pfxs_offset, peers_offset, type) =\
            struct.unpack("=%dsLLQQc" % strlen, msg[2:2+msglen])
        res = {
            'collector': collector,
            'time': time,
            'peers_cnt': peers_cnt,
            'pfxs_offset': pfxs_offset,
            'peers_offset': peers_offset,
            'type': type,
        }
        if type == b'D':
            # there are an extra couple of fields in a diff message
            (sync_md_offset, parent_time) = struct.unpack("=QL", msg[2+msglen:])
            res['sync_md_offset'] = sync_md_offset
            res['parent_time'] = parent_time
        return res

    @staticmethod
    def serialize_gmd_msg(view_time, last_sync_offset, members):
        msg = struct.pack("=LH", view_time, len(members))
        parts = []
        type = None
        for member in members:
            if not type:
                type = member['type']
            assert member['type'] == type, "Inconsistent type: %s" % members
            coll = member['collector']
            mmsg = struct.pack("=H", len(coll)) + \
                struct.pack("=%dsLLQQc" % len(coll),
                            coll,
                            view_time,
                            member['peers_cnt'],
                            member['pfxs_offset'],
                            member['peers_offset'],
                            member['type'])
            if member['type'] == b'D':
                mmsg += struct.pack("=QL",
                                    member['sync_md_offset'],
                                    member['parent_time'])
            parts.append(mmsg)
        parts.append(struct.pack("=q", last_sync_offset))
        return msg + b''.join(parts)


def main():
    parser = argparse.ArgumentParser(description="""
    Watches the members and metadata topics of a BGPView Kafka stream and
    signals availability of global views
    """)
    parser.add_argument('-b',  '--brokers',
                        nargs='?', required=True,
                        help='Comma-separated list of broker URIs')
    parser.add_argument('-k',  '--timeseries-config',
                        nargs='?', required=True,
                        help='libtimeseries backend config')
    parser.add_argument('-g',  '--consumer-group',
                        nargs='?', required=False,
                        help='consumer group to use when reading from kafka')
    parser.add_argument('-c', '--pub-channel',
                        nargs='?', required=False,
                        help='Channel to publish Global Metadata messages to')
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
