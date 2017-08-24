"""
Custom xmlrpc handling for Koji
"""

import six
import six.moves.xmlrpc_client as xmlrpc_client
import types


# duplicate a few values that we need
getparser = xmlrpc_client.getparser
loads = xmlrpc_client.loads
Fault = xmlrpc_client.Fault


class ExtendedMarshaller(xmlrpc_client.Marshaller):

    dispatch = xmlrpc_client.Marshaller.dispatch.copy()

    def dump_generator(self, value, write):
        dump = self.__dump
        write("<value><array><data>\n")
        for v in value:
            dump(v, write)
        write("</data></array></value>\n")
    dispatch[types.GeneratorType] = dump_generator

    MAXI8 = 2 ** 64 - 1
    MINI8 = -2 ** 64

    def dump_i8(self, value, write):
        # python2's xmlrpclib doesn't support i8 extension for marshalling,
        # but can unmarshall it correctly.
        if (value > self.MAXI8 or value < self.MINI8):
            raise OverflowError("long int exceeds XML-RPC limits")
        elif (value > xmlrpc_client.MAXINT or
                value < xmlrpc_client.MININT):
            write("<value><i8>")
            write(str(int(value)))
            write("</i8></value>\n")
        else:
            write("<value><int>")
            write(str(int(value)))
            write("</int></value>\n")
    dispatch[types.LongType] = dump_i8
    dispatch[types.IntType] = dump_i8

    # we always want to allow None
    def dump_nil(self, value, write):
        write("<value><nil/></value>")
    dispatch[type(None)] = dump_nil


def dumps(params, methodname=None, methodresponse=None, encoding=None,
          allow_none=1, marshaller=None):
    """encode an xmlrpc request or response

    Differences from the xmlrpclib version:
        - allow_none is on by default
        - uses our ExtendedMarshaller by default
        - option to specify marshaller
    """

    if isinstance(params, Fault):
        methodresponse = 1
    elif not isinstance(params, tuple):
        raise TypeError('params must be a tuple of Fault instance')
    elif methodresponse and len(params) != 1:
        raise ValueError('response tuple must be a singleton')

    if not encoding:
        encoding = "utf-8"

    if marshaller is not None:
        m = marshaller(encoding)
    else:
        m = ExtendedMarshaller(encoding, allow_none)

    data = m.dumps(params)

    if encoding != "utf-8":
        xmlheader = "<?xml version='1.0' encoding='%s'?>\n" % str(encoding)
    else:
        xmlheader = "<?xml version='1.0'?>\n"  # utf-8 is default

    # standard XML-RPC wrappings
    if methodname:
        # a method call
        if six.PY2 and isinstance(methodname, six.text_type):
            # Do we need this?
            methodname = methodname.encode(encoding, 'xmlcharrefreplace')
        parts = (
            xmlheader,
            "<methodCall>\n"
            "<methodName>", methodname, "</methodName>\n",
            data,
            "</methodCall>\n"
            )
    elif methodresponse:
        # a method response, or a fault structure
        parts = (
            xmlheader,
            "<methodResponse>\n",
            data,
            "</methodResponse>\n"
            )
    else:
        return data  # return as is
    return ''.join(parts)
