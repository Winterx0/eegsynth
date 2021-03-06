#!/usr/bin/env python

# Sequencer acts as a basic timer and sequencer
#
# This software is part of the EEGsynth project, see https://github.com/eegsynth/eegsynth
#
# Copyright (C) 2017-2018 EEGsynth project
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import ConfigParser  # this is version 2.x specific,on version 3.x it is called "configparser" and has a different API
import argparse
import math
import numpy as np
import os
import redis
import sys
import threading
import time

if hasattr(sys, 'frozen'):
    basis = sys.executable
elif sys.argv[0] != '':
    basis = sys.argv[0]
else:
    basis = './'
installed_folder = os.path.split(basis)[0]

# eegsynth/lib contains shared modules
sys.path.insert(0, os.path.join(installed_folder, '../../lib'))
import EEGsynth

parser = argparse.ArgumentParser()
parser.add_argument("-i", "--inifile", default=os.path.join(installed_folder, os.path.splitext(os.path.basename(__file__))[0] + '.ini'), help="optional name of the configuration file")
args = parser.parse_args()

config = ConfigParser.ConfigParser()
config.read(args.inifile)

try:
    r = redis.StrictRedis(host=config.get('redis', 'hostname'), port=config.getint('redis', 'port'), db=0)
    response = r.client_list()
except redis.ConnectionError:
    print "Error: cannot connect to redis server"
    exit()

# combine the patching from the configuration file and Redis
patch = EEGsynth.patch(config, r)
del config

# this determines how much debugging information gets printed
debug = patch.getint('general', 'debug')

# this is to prevent two messages from being sent at the same time
lock = threading.Lock()

# this can be used to selectively show parameters that have changed
previous = {}
def show_change(key, val):
    if (key in previous and previous[key] != val) or (key not in previous):
        print key, "=", val
    previous[key] = val


class SequenceThread(threading.Thread):
    def __init__(self, redischannel, key):
        threading.Thread.__init__(self)
        self.redischannel = redischannel
        self.key = key
        self.sequence = []
        self.transpose = 0
        self.duration = 0
        self.step = 0
        self.running = True

    def setSequence(self, sequence):
        with lock:
            self.sequence = sequence

    def setTranspose(self, transpose):
        with lock:
            self.transpose = transpose

    def setDuration(self, sequence):
        with lock:
            self.duration = duration

    def stop(self):
        self.running = False

    def run(self):
        pubsub = r.pubsub()
        pubsub.subscribe('SEQUENCER_UNBLOCK')  # this message unblocks the redis listen command
        pubsub.subscribe(self.redischannel)    # this message contains the note
        while self.running:
            for item in pubsub.listen():
                if not self.running or not item['type'] == 'message':
                    break
                if item['channel'] == self.redischannel:
                    if len(self.sequence) > 0:
                        val = self.sequence[self.step % len(self.sequence)]
                        # the sequence can consist of a list of values or a list of Redis channels
                        try:
                            val = float(val)
                        except:
                            val = r.get(val)
                        val = val + self.transpose
                        patch.setvalue(self.key, val, debug=debug)
                        if val>=1.:
                            # send it as sequencer.noteXXX with value 1.0
                            key = '%s%03d' % (self.key, val)
                            val = 1.
                            patch.setvalue(key, val, debug=debug, duration=self.duration)
                        self.step = (self.step + 1) % len(self.sequence)
                        if debug>0:
                            print "step", self.step, self.key, val

# these scale and offset parameters are used to map between Redis and internal values
scale_select     = patch.getfloat('scale', 'select',     default=127.)
scale_transpose  = patch.getfloat('scale', 'transpose',  default=127.)
scale_note       = patch.getfloat('scale', 'note',       default=1.)
scale_duration   = patch.getfloat('scale', 'duration',   default=1.)
offset_select    = patch.getfloat('offset', 'select',    default=0.)
offset_transpose = patch.getfloat('offset', 'transpose', default=0.)
offset_note      = patch.getfloat('offset', 'note',      default=0.)
offset_duration  = patch.getfloat('offset', 'duration',  default=0.)

# this is the clock signal for the sequence
clock = patch.getstring('sequence', 'clock')

# the notes will be sent to Redis using this key
key = "{}.note".format(patch.getstring('output', 'prefix'))

# create and start the thread for the output
sequencethread = SequenceThread(clock, key)
sequencethread.start()

if debug > 0:
    show_change('scale_select',     scale_select)
    show_change('scale_transpose',  scale_transpose)
    show_change('scale_note',       scale_note)
    show_change('scale_duration',   scale_duration)
    show_change('offset_select',    offset_select)
    show_change('offset_transpose', offset_transpose)
    show_change('offset_note',      offset_note)
    show_change('offset_duration',  offset_duration)

try:
    while True:
        # measure the time to correct for the slip
        now = time.time()

        if debug > 1:
            print 'loop'

        # the selected pattern should be a integer between 0 and 127
        select = patch.getfloat('sequence', 'select', default=0)
        select = EEGsynth.rescale(select, slope=scale_select, offset=offset_select)
        select = int(select)

        # get the corresponding sequence as a single string
        try:
            sequence = patch.getstring('sequence', "pattern{:d}".format(select), multiple=True)
        except:
            sequence = []

        transpose = patch.getfloat('sequence', 'transpose', default=0.)
        transpose = EEGsynth.rescale(transpose, slope=scale_transpose, offset=offset_transpose)

        duration = patch.getfloat('general', 'duration', default=0.)
        duration = EEGsynth.rescale(duration, slope=scale_duration, offset=offset_duration)

        sequencethread.setSequence(sequence)
        sequencethread.setTranspose(transpose)
        sequencethread.setDuration(duration)

        if debug > -1:
            # show the parameters whose value has changed
            show_change("select",    select)
            show_change("sequence",  sequence)
            show_change("transpose", transpose)
            show_change("duration",  duration)

        elapsed = time.time() - now
        naptime = patch.getfloat('general', 'delay') - elapsed
        if naptime > 0:
            time.sleep(naptime)

except KeyboardInterrupt:
    try:
        print "Disabling last note"
        patch.setvalue(key, 0.)
    except:
        pass
    print "Closing threads"
    sequencethread.stop()
    r.publish('SEQUENCER_UNBLOCK', 1)
    sequencethread.join()
    sys.exit()
