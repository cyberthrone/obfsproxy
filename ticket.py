#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Implements a subset of session tickets as proposed for TLS in RFC 5077:
https://tools.ietf.org/html/rfc5077

The format of a 112-byte ticket:
    +------------+------------------+--------------+
    | 16-byte IV | 64-byte E(state) | 32-byte HMAC |
    +------------+------------------+--------------+

The 64-byte encrypted state contains the following data:
+-------------------+--------------------+--------------------+-------------+
| 4-byte issue date | 18-byte identifier | 32-byte master key | 10-byte pad |
+-------------------+--------------------+--------------------+-------------+
"""

import os
import time
import const
import pickle
import base64
import struct
import datetime

from Crypto.Cipher import AES
from Crypto.Hash import HMAC
from Crypto.Hash import SHA256

import obfsproxy.common.log as logging

import mycrypto
import util

log = logging.get_obfslogger()

# Length of the IV which is used for AES-CBC.
IV_LENGTH = 16

# Length of the HMAC used to authenticate the ticket.
HMAC_KEY_LENGTH = 32

# Length of the AES key used to encrypt the ticket.
AES_KEY_LENGTH = 16

# Must be a multiple of 16 bytes due to AES' block size.
IDENTIFIER = "ScrambleSuitTicket"

HMACKey = AESKey = creationTime = None


def rotateKeys( ):
    """The keys used to encrypt and authenticate tickets are rotated
    periodically.  New keys are created but the old keys are still cached for
    the next period to validate previously issued tickets."""

    global HMACKey
    global AESKey
    global creationTime

    log.info("Rotating session ticket keys.")

    HMACKey = mycrypto.strong_random(HMAC_KEY_LENGTH)
    AESKey = mycrypto.strong_random(AES_KEY_LENGTH)
    creationTime = int(time.time())

    try:
        with open(const.KEY_STORE, "wb") as fd:
            pickle.dump([creationTime, HMACKey, AESKey], fd)
            fd.close()
    except IOError as e:
        log.error("Error opening ticket key file: %s." % e)


def loadKeys( ):
    """Try to load the AES and HMAC key used to encrypt and authenticate
    tickets from the key store."""

    global HMACKey
    global AESKey
    global creationTime

    log.info("Reading session ticket keys k_S from `%s'." % const.KEY_STORE)

    # If the key store does not exist (yet), it must be created.
    if not os.path.exists(const.KEY_STORE):
        rotateKeys()
        return

    try:
        with open(const.KEY_STORE, "rb") as fd:
            creationTime, HMACKey, AESKey = pickle.load(fd)
            fd.close()
    except IOError as e:
        log.error("Error opening ticket key file: %s." % e)


def checkKeys( ):
    """Load the AES and the HMAC key if they are not defined yet.  If they are
    expired, rotate the keys."""

    if (HMACKey is None) or (AESKey is None):
        loadKeys()

    if (int(time.time()) - creationTime) > const.KEY_ROTATION_TIME:
        rotateKeys()


def decrypt( ticket ):
    """Verifies the validity, decrypts and finally returns the given potential
    ticket as a ProtocolState object.  If the ticket is invalid, `None' is
    returned."""

    assert len(ticket) == const.TICKET_LENGTH

    global HMACKey
    global AESKey
    global creationTime

    log.debug("Attempting to decrypt and verify %d-byte ticket." % len(ticket))

    checkKeys()

    # Verify if the HMAC is correct.
    hmac = HMAC.new(HMACKey, ticket[0:80], digestmod=SHA256).digest()
    if hmac != ticket[80:const.TICKET_LENGTH]:
        log.debug("The ticket's HMAC is invalid.  Probably not a ticket.")
        return None

    # Decrypt ticket to obtain state.
    aes = AES.new(AESKey, mode=AES.MODE_CBC, IV=ticket[0:16])
    plainTicket = aes.decrypt(ticket[16:80])

    issueDate = struct.unpack('I', plainTicket[0:4])[0]
    identifier = plainTicket[4:22]
    masterKey = plainTicket[22:54]

    if not (identifier == IDENTIFIER):
        log.error("The HMAC is valid but the identifier could not be " \
                "found.  The ticket could be corrupt.")
        return None

    return ProtocolState(masterKey, issueDate=issueDate)


class ProtocolState( object ):
    """Describes the protocol state of a ScrambleSuit server which is part of a
    session ticket.  The state can be used to bootstrap a ScrambleSuit session
    without the client unlocking the puzzle."""

    def __init__( self, masterKey, issueDate=int(time.time()) ):
        self.identifier = IDENTIFIER
        self.masterKey = masterKey
        self.issueDate = issueDate
        # Pad to multiple of 16 bytes due to AES' block size.
        self.pad = "\0\0\0\0\0\0\0\0\0\0"


    def isValid( self ):
        """Returns `True' if the protocol state is valid, i.e., if the life
        time has not expired yet.  Otherwise, `False' is returned."""

        assert self.issueDate

        lifetime = int(time.time()) - self.issueDate
        if lifetime > const.SESSION_TICKET_LIFETIME:
            log.debug("The ticket expired %s ago." % \
                    str(datetime.timedelta(seconds=lifetime - \
                    const.SESSION_TICKET_LIFETIME)))
            return False

        log.debug("The ticket is still valid for %s." %  \
                    str(datetime.timedelta(seconds= \
                    const.SESSION_TICKET_LIFETIME - lifetime)))

        return True


    def __repr__( self ):

        return struct.pack('I', self.issueDate) + self.identifier + \
                self.masterKey + self.pad


class SessionTicket( object ):
    """Encapsulates a session ticket which can be used by the client to gain
    access to a ScrambleSuit server without solving the served puzzle."""

    def __init__( self, masterKey ):
        """Initialize a new session ticket which contains `masterKey'. The
        parameter `symmTicketKey' is used to encrypt the ticket and
        `hmacTicketKey' is used to authenticate the ticket when issued."""

        assert len(masterKey) == const.MASTER_KEY_LENGTH

        checkKeys()

        # Initialization vector for AES-CBC.
        self.IV = mycrypto.strong_random(IV_LENGTH)

        # The server's actual (encrypted) protocol state.
        self.state = ProtocolState(masterKey)

        # AES and HMAC key to protect the ticket.
        self.symmTicketKey = AESKey
        self.hmacTicketKey = HMACKey


    def issue( self ):
        """Encrypt and authenticate the ticket and return the result which is
        ready to be sent over the wire. In particular, the ticket name (for
        bookkeeping) as well as the actual encrypted ticket is returned."""

        self.state.issueDate = int(time.time())

        # Encrypt the protocol state.
        aes = AES.new(self.symmTicketKey, mode=AES.MODE_CBC, IV=self.IV)
        state = repr(self.state)
        assert (len(state) % AES.block_size) == 0
        cryptedState = aes.encrypt(state)

        # Authenticate ticket name, IV and the encrypted state.
        hmac = HMAC.new(self.hmacTicketKey, self.IV + \
                cryptedState, digestmod=SHA256).digest()

        ticket = self.IV + cryptedState + hmac

        log.debug("Returning %d-byte ticket." % (len(self.IV) +
            len(cryptedState) + len(hmac)))

        return ticket


# Alias class name in order to provide a more intuitive API.
new = SessionTicket


# Give ScrambleSuit server operators a way to manually issue new session
# tickets for out-of-band distribution.
if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_file", type=str, \
            help="The file, the newly issued ticket is written to.")
    args = parser.parse_args()

    print "[+] Generating new session ticket."
    masterKey = mycrypto.strong_random(const.MASTER_KEY_LENGTH)
    ticketObj = SessionTicket(masterKey)
    ticket = ticketObj.issue()

    print "[+] Writing new session ticket to `%s'." % args.ticket_file
    util.writeToFile(base64.b32encode(masterKey + ticket) + '\n', \
            args.ticket_file)

    print "[+] Success."
