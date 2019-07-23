#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import sys
import typing
import asyncio
import decimal
import logging
import argparse
import contextlib
import argparse_utils
import pyuavcan
from . import _util, _subsystems
from ._yaml import YAMLLoader
from ._base import Command, SubsystemFactory


_logger = logging.getLogger(__name__)


class CallCommand(Command):
    @property
    def names(self) -> typing.Sequence[str]:
        return ['call']

    @property
    def help(self) -> str:
        return '''
Invoke a service using a specified request object and print the response.
The local node will also publish heartbeat and respond to GetInfo.
'''.strip()

    @property
    def examples(self) -> typing.Optional[str]:
        return '''
pyuavcan call 42 uavcan.node.GetInfo.1.0 '{}'
'''.strip()

    @property
    def subsystem_factories(self) -> typing.Sequence[SubsystemFactory]:
        return [
            _subsystems.node.NodeFactory(node_name_suffix=self.names[0], allow_anonymous=False),
            _subsystems.formatter.FormatterFactory(),
        ]

    def register_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            'server_node_id',
            metavar='SERVER_NODE_ID',
            type=int,
            help=f'''
The node ID of the server that the request will be sent to.
Valid values range from zero (inclusive) to a transport-specific upper limit.
'''.strip())
        parser.add_argument(
            'service_spec',
            metavar='[SERVICE_ID.]FULL_SERVICE_TYPE_NAME.MAJOR.MINOR',
            help='''
The full service type name with version and optional service-ID.
The service-ID can be omitted if a fixed one is defined for the data type.
Examples:
    123.uavcan.node.ExecuteCommand.1.0 (using service-ID 123)
    uavcan.node.ExecuteCommand.1.0 (using the fixed service-ID 435)
'''.strip())
        parser.add_argument(
            'field_spec',
            metavar='YAML_FIELDS',
            type=YAMLLoader().load,
            help='''
The YAML (or JSON, which is a subset of YAML)-formatted contents of the
request object. Missing fields will be left at their default values.
Use empty dict as "{}" to construct a default-initialized request object.
For more info about the YAML representation, read the PyUAVCAN documentation
on builtin-based representations.
'''.strip())
        parser.add_argument(
            '--timeout', '-T',
            metavar='REAL',
            type=float,
            default=pyuavcan.presentation.DEFAULT_SERVICE_REQUEST_TIMEOUT,
            help=f'''
Request timeout; i.e., how long to wait for the response before giving up.
Default: %(default)s
'''.strip())
        parser.add_argument(
            '--priority',
            default=pyuavcan.presentation.DEFAULT_PRIORITY,
            action=argparse_utils.enum_action(pyuavcan.transport.Priority),
            help='''
Priority of the request transfer. Applies to the heartbeat as well.
Default: %(default)s
'''.strip())
        parser.add_argument(
            '--transfer-id',
            default=0,
            type=int,
            help='''
The transfer-ID value to use for the request transfer.
The value will be also used as the initial value of heartbeat publications.
You will need to increment this value manually if you're invoking the same
service on the same node more than once in a short period of time.

The protocol stack will compute the modulus automatically as necessary; e.g.,
in the case of a transport where the transfer-ID modulo equals 32, supplying
123 here would result in the transfer-ID value of 123 %% 32 = 27.

Default: %(default)s
'''.strip())
        parser.add_argument(
            '--with-metadata', '-M',
            action='store_true',
            help='''
Emit metadata together with the response.
The metadata fields will be contained under the key "_metadata_".
'''.strip())

    def execute(self, args: argparse.Namespace, subsystems: typing.Sequence[object]) -> int:
        return asyncio.get_event_loop().run_until_complete(_do_execute(args, subsystems))


async def _do_execute(args: argparse.Namespace, subsystems: typing.Sequence[object]) -> int:
    import pyuavcan.application
    node, formatter = subsystems
    assert isinstance(node, pyuavcan.application.Node)
    assert callable(formatter)

    with contextlib.closing(node):
        node.heartbeat_publisher.priority = args.priority
        node.heartbeat_publisher.publisher.transfer_id_counter.override(args.transfer_id)

        # Construct the request object.
        service_id, dtype = _util.construct_port_id_and_type(args.service_spec)
        if not issubclass(dtype, pyuavcan.dsdl.ServiceObject):
            raise ValueError(f'Expected a service type; got this: {dtype.__name__}')

        request = pyuavcan.dsdl.update_from_builtin(dtype.Request(), args.field_spec)
        _logger.info('Request object: %r', request)

        # Initialize the client instance.
        client = node.make_client(dtype, service_id, args.server_node_id)
        client.response_timeout = args.timeout
        client.priority = args.priority
        client.transfer_id_counter.override(args.transfer_id)

        request_ts_transport: typing.Optional[pyuavcan.transport.Timestamp] = None

        def on_transfer_feedback(fb: pyuavcan.transport.Feedback) -> None:
            nonlocal request_ts_transport
            request_ts_transport = fb.first_frame_transmission_timestamp

        client.output_transport_session.enable_feedback(on_transfer_feedback)

        # Ready to do the job now.
        node.start()
        request_ts_application = pyuavcan.transport.Timestamp.now()
        result = await client.call_with_transfer(request)
        response_ts_application = pyuavcan.transport.Timestamp.now()

        # Print the results.
        if result is None:
            print(f'The request has timed out after {client.response_timeout:0.1f} seconds', file=sys.stderr)
            return 1
        else:
            if not request_ts_transport:  # pragma: no cover
                request_ts_transport = request_ts_application
                _logger.error('The transport implementation is misbehaving: feedback was never emitted; '
                              'falling back to software timestamping. '
                              'Please submit a bug report. Involved instances: node=%r, client=%r, result=%r',
                              node, client, result)

            response, transfer = result

            transport_duration = transfer.timestamp.monotonic - request_ts_transport.monotonic
            application_duration = response_ts_application.monotonic - request_ts_application.monotonic
            _logger.info('Request duration [second]: '
                         'transport layer: %.6f, application layer: %.6f, application layer overhead: %.6f',
                         transport_duration, application_duration, application_duration - transport_duration)

            _print_result(service_id=service_id,
                          response=response,
                          transfer=transfer,
                          formatter=formatter,
                          request_transfer_ts=request_ts_transport,
                          app_layer_duration=application_duration,
                          with_metadata=args.with_metadata)
    return 0


def _print_result(service_id:          int,
                  response:            pyuavcan.dsdl.CompositeObject,
                  transfer:            pyuavcan.transport.TransferFrom,
                  formatter:           _subsystems.formatter.Formatter,
                  request_transfer_ts: pyuavcan.transport.Timestamp,
                  app_layer_duration:  decimal.Decimal,
                  with_metadata:       bool) -> None:
    bi: typing.Dict[str, typing.Any] = {}  # We use updates to ensure proper dict ordering: metadata before data
    if with_metadata:
        rtt_qnt = decimal.Decimal('0.000001')
        bi.update(_util.convert_transfer_metadata_to_builtin(
            transfer,
            roundtrip_time={
                'transport_layer':   (transfer.timestamp.monotonic - request_transfer_ts.monotonic).quantize(rtt_qnt),
                'application_layer': app_layer_duration.quantize(rtt_qnt),
            }
        ))
    bi.update(pyuavcan.dsdl.to_builtin(response))

    print(formatter({
        service_id: bi,
    }))