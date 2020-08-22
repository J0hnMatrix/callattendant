#!/usr/bin/python
#
# file: callattendant.py
#
# Copyright 2018 Bruce Schubert <bruce@emxsys.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import sys
import queue
import sqlite3
import time
from pprint import pprint
from datetime import datetime

from config import Config
from screening.calllogger import CallLogger
from screening.callscreener import CallScreener
from hardware.modem import Modem
from hardware.indicators import ApprovedIndicator, BlockedIndicator
from messaging.voicemail import VoiceMail
import userinterface.webapp as webapp


class CallAttendant(object):
    """The CallAttendant provides call logging and call screening services."""

    def __init__(self, config):
        """
        The constructor initializes and starts the Call Attendant.
            :param config: the application config dict
        """
        # The application-wide configuration
        self.config = config

        # Open the database
        db_path = None
        if self.config["TESTING"]:
            self.db = sqlite3.connect(":memory:")
        else:
            self.db = sqlite3.connect(self.config['DB_FILE'])

        # Create a synchronized queue for incoming callers from the modem
        self._caller_queue = queue.Queue()

        #  Initialize the visual indicators (LEDs)
        self.approved_indicator = ApprovedIndicator()
        self.blocked_indicator = BlockedIndicator()

        # Screening subsystem
        self.logger = CallLogger(self.db, self.config)
        self.screener = CallScreener(self.db, self.config)

        #  Hardware subsystem
        #  Create (and starts) the modem with callback functions
        self.modem = Modem(self.config, self.handle_caller)

        # Messaging subsystem
        self.voice_mail = VoiceMail(self.db, self.config, self.modem)
        # The message indicator is a singleton and is shared with the webapp
        self.config["MESSAGE_INDICATOR_LED"] = self.voice_mail.message_indicator

        # Start the User Interface subsystem (Flask)
        # Skip if we're running functional tests, because when testing
        # we use a memory database which can't be shared between threads.
        if not self.config["TESTING"]:
            print("Staring the Flask webapp")
            webapp.start(self.config)

    def handle_caller(self, caller):
        """
        A callback function used by the modem that places the given
        caller object into the synchronized queue for processing by the
        run method.
            :param caller: a dict object with caller ID information
        """
        if self.config["DEBUG"]:
            print("Adding to caller queue:")
            pprint(caller)
        self._caller_queue.put(caller)

    def run(self):
        """
        Processes incoming callers by logging, screening, blocking
        and/or recording messages.
        """
        # Get relevant config settings
        screening_mode = self.config['SCREENING_MODE']
        block = self.config.get_namespace("BLOCK_")
        blocked = self.config.get_namespace("BLOCKED_")
        screened = self.config.get_namespace("SCREENED_")
        permitted = self.config.get_namespace("PERMITTED_")
        blocked_greeting_file = blocked['greeting_file']
        screened_greeting_file = screened['greeting_file']
        permitted_greeting_file = permitted['greeting_file']

        # Instruct the modem to start feeding calls into the caller queue
        self.modem.handle_calls()

        # Process incoming calls
        while 1:
            try:
                # Wait (blocking) for a caller
                caller = self._caller_queue.get()

                # An incoming call has occurred, log it
                number = caller["NMBR"]
                phone_no = "{}-{}-{}".format(number[0:3], number[3:6], number[6:])
                print("Incoming call from {}".format(phone_no))

                # Vars used in the call screening
                caller_permitted = False
                caller_screened = False
                caller_blocked = False
                action = ""
                reason = ""

                # Check the whitelist
                if "whitelist" in screening_mode:
                    print("> Checking whitelist(s)")
                    is_whitelisted, reason = self.screener.is_whitelisted(caller)
                    if is_whitelisted:
                        caller_permitted = True
                        action = "Permitted"
                        self.approved_indicator.blink()

                # Now check the blacklist if not preempted by whitelist
                if not caller_permitted and "blacklist" in screening_mode:
                    print("> Checking blacklist(s)")
                    is_blacklisted, reason = self.screener.is_blacklisted(caller)
                    if is_blacklisted:
                        caller_blocked = True
                        action = "Blocked"
                        self.blocked_indicator.blink()

                if not caller_permitted and not caller_blocked:
                    caller_screened = True
                    action = "Screened"
                    self.approved_indicator.blink()

                # Log every call to the database (and console)
                call_no = self.logger.log_caller(caller, action, reason)
                print("--> {} {}: {}".format(phone_no, action, reason))

                # Gather the data used to answer the call
                if caller_permitted:
                    actions = permitted["actions"]
                    greeting = permitted_greeting_file
                    rings_before_answer = permitted["rings_before_answer"]
                elif caller_screened:
                    actions = screened["actions"]
                    greeting = screened_greeting_file
                    rings_before_answer = screened["rings_before_answer"]
                elif caller_blocked:
                    actions = blocked["actions"]
                    greeting = blocked_greeting_file
                    rings_before_answer = blocked["rings_before_answer"]

                # Wait for the callee to answer the phone, if configured to do so
                ok_to_answer = True
                ring_count = 1  # Already had at least 1 ring to get here
                while ring_count < rings_before_answer:
                    # In North America, the standard ring cadence is "2-4", or two seconds
                    # of ringing followed by four seconds of silence (33% Duty Cycle).
                    if self.modem.ring_event.wait(7):
                        ring_count = ring_count + 1
                        print(" > > > Ring count: {}".format(ring_count))
                    else:
                        # wait timeout; assume ringing has stopped before the ring count
                        # was reached because either the callee answered or caller hung up.
                        ok_to_answer = False
                        print(" > > > Ringing stopped: Caller hung up or callee answered")
                        break

                # Answer the call!
                if ok_to_answer and len(actions) > 0:
                    self.answer_call(actions, greeting, call_no, caller)

            except Exception as e:
                pprint(e)
                print("** Error running callattendant. Exiting.")
                return 1

    def answer_call(self, actions, greeting, call_no, caller):
        """
        Answer the call with the supplied actions, e.g, voice mail,
        record message, or simply pickup and hang up.
        """
        # Go "off-hook" - Acquires a lock on the modem - MUST follow with hang_up()
        if self.modem.pick_up():
            try:
                # Play greeting
                if "greeting" in actions:
                    print(">> Playing greeting...")
                    self.modem.play_audio(greeting)

                # Record message
                if "record_message" in actions:
                    print(">> Recording message...")
                    self.voice_mail.record_message(call_no, caller)

                # Enter voice mail menu
                elif "voice_mail" in actions:
                    print(">> Starting voice mail...")
                    self.voice_mail.voice_messaging_menu(call_no, caller)

            except RuntimeError as e:
                print("** Error handling a blocked caller: {}".format(e))

            finally:
                # Go "on-hook"
                self.modem.hang_up()


def make_config(filename=None):
    '''Creates the config dictionary for this application/module.
        :param filename: the filename of a python configuration file.
            This can either be an absolute filename or a filename
            relative to this module's location.
        :return: a config dict'''

    # Establish the default configuration settings
    root_path = os.path.dirname(os.path.realpath(__file__))
    data_path = os.path.expandvars("$HOME/.callattendant")   # could be a cmdline arg

    # Create the default configuration
    config = Config(root_path, data_path)

    # Load the supplied config file, which may overwrite the defaults
    if filename is not None:
        config.from_pyfile(filename)
        config["CONFIG_FILE"] = filename

    # Build abs paths for all the file based settings
    config.normalize_paths()

    # Ensure data folders exist
    if not os.path.isdir(data_path):
        print("The DATA_PATH folder is not present. Creating {}".format(data_path))
        os.makedirs(data_path)
    msgpath = config["VOICE_MAIL_MESSAGE_FOLDER"]
    if not os.path.isdir(msgpath):
        print("The VOICE_MAIL_MESSAGE_FOLDER folder is not present. Creating {}".format(msgpath))
        os.makedirs(msgpath)

    # Always print the configuration
    config.pretty_print()

    return config


def get_args(argv):
    """Get arguments from the command line
        :param argv: sys.argv
        :return: configfile
    """
    import sys
    import getopt
    syntax = 'Usage: python callattendant.py -c [FILE]'
    configfile = None
    try:
        opts, args = getopt.getopt(argv[1:], "hc:", ["help", "config="])
    except getopt.GetoptError:
        print(syntax)
        sys.exit(2)
    for opt, arg in opts:
        if opt in ("-h", "--help"):
            print(syntax)
            print("-c, --config=[FILE]\tload a python configuration file")
            print("-h, --help\t\tdisplays this help text")
            sys.exit()
        elif opt in ("-c", "--config"):
            configfile = arg
    return configfile


def main(argv):
    """Create and run the call attendent application"""

    # Process command line arguments
    config_file = get_args(argv)
    # Create the application-wide config dict
    config = make_config(config_file)
    # Ensure all specified files exist and that values are conformant
    if not config.validate():
        print("Configuration is invalid. Please check {}".format(config_file))
        return 1

    # Create and start the application
    app = CallAttendant(config)
    app.run()
    return 0


if __name__ == '__main__':

    sys.exit(main(sys.argv))