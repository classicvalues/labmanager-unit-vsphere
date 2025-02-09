import datetime

from web.settings import Settings as settings
from sanic.response import json as sjson
import sanic.response
from sanic.exceptions import abort
from sanic import Blueprint

import web.modeltr as data

import web.middleware.obtain_request
import web.module.capabilities as capabilities
import sanic.exceptions
import json

import sys
import threading
import asyncio
import logging
import web.enhanced_logging as el


logger = logging.getLogger(__name__)

machines = Blueprint('machines')


async def check_payload_deploy(request):
    # tests if 'json_params' are in headers and contains 'labels'
    if 'json_params' not in request.headers or 'labels' not in request.headers['json_params']:
        raise sanic.exceptions.InvalidUsage('malformed input json data, \'labels\' must be specified')

    labels = request.headers['json_params']['labels']
    template_labels_list = [label for label in labels if label.startswith('template:')]

    # test if labels contain 'template' label(s)
    if not template_labels_list:
        raise sanic.exceptions.InvalidUsage(f'label specification {labels} does not contain \'template\' label')

    # test if labels contain only one 'template' label
    if len(template_labels_list) > 1:
        raise sanic.exceptions.InvalidUsage(f'label specification {labels} contains multiple \'template\' labels')

    # test if template is supported by unit
    supported_labels = settings.app['labels']
    for template_label in template_labels_list:
        if template_label not in supported_labels:
            raise sanic.exceptions.InvalidUsage(f'\'{template_label}\' label is not supported by this unit')


@machines.middleware('request')
async def obtain_request(request):
    logger.debug("Request obtained: {}".format(request))


@machines.exception(sanic.exceptions.InvalidUsage)
def handle_exceptions(request, exception):
    return {
            'exception': '|'.join(exception.args),
    }


async def check_resources():
    await capabilities.Capabilities.fetch(forced=True)
    if capabilities.Capabilities.get_free_slots() < 1:
        raise sanic.exceptions.InvalidUsage(
                'no extra machine can be currently deployed, \
please wait till another slot is to be freed'
        )


@machines.route('/machines', methods=['POST'])
@el.log_func_boundaries
async def machine_deploy(request):
    el.log_d(request, "POST /machines wanted by: {}".format(request.headers.get("AUTHORISED_LOGIN", "<n/a>")))
    await check_payload_deploy(request)
    labels = request.headers['json_params']['labels']
    await check_resources()
    el.log_d(request, "attempting to create db session")
    with data.Connection.use() as conn:
        new_request = data.Request(type=data.RequestType.DEPLOY)
        new_request.save(conn=conn)
        el.log_d(request, "new request saved")
        if settings.app['service']['personalised']:
            new_machine = data.Machine(
                labels=labels,
                requests=[new_request.id],
                owner=request.headers["AUTHORISED_LOGIN"],
                created_at=datetime.datetime.now()
            )
        else:
            new_machine = data.Machine(
                labels=labels,
                requests=[new_request.id],
                created_at=datetime.datetime.now()
            )
        new_machine.save(conn=conn)
        el.log_d(request, "new machine saved")

        new_request.machine = str(new_machine.id)
        new_request.save(conn=conn)
        el.log_d(request, "new request saved again")

        # begin machine preparation
        data.Action(type='deploy', request=new_request.id).save(conn=conn)
        el.log_d(request, "new action saved")

    return {
            'request_id': '{}'.format(new_request.id),
            'is_last': False
    }


async def get_machines(request, connection, **kwargs):
    raw_args = request.raw_args
    if 'flt' in kwargs:
        raw_args = {**raw_args, **kwargs['flt']}
    if settings.app['service']['personalised'] and \
       request.headers.get("AUTHORISED_AS", "None") == "user":
        return data.Machine.get(
            {**raw_args, **{'owner': request.headers["AUTHORISED_LOGIN"]}},
            conn=connection
        )
    else:
        return data.Machine.get(raw_args, conn=connection)


async def show_hidden_strings(request):
    return request.headers.get("AUTHORISED_AS", "None") == "admin"


@machines.route('/machines', methods=['GET'])
async def machines_get_info(request):
    for key in request.raw_args.keys():
        if key not in ['state']:
            raise sanic.exceptions.InvalidUsage(
                'malformatted parameter: {}'.format(key)
            )

    with data.Connection.use() as conn:
        await asyncio.sleep(0.1)
        machines = await get_machines(request, conn)
        output = []
        for machine in machines:
            output.append({
                **machine.to_dict(show_hidden=await show_hidden_strings(request)),
                **{'id': machine.id}
            })

    return {
            'result': output,
            'is_last': True
    }


@machines.route('/machines/<machine_id>', methods=['GET'])
async def machine_get_info(request, machine_id):
    logger.debug('Current thread name: {}'. format(threading.current_thread().name))
    with data.Connection.use() as conn:
        await asyncio.sleep(0.1)
        try:
            req = (await get_machines(request, conn, flt={'_id': machine_id})).first()
            result = req.to_dict(show_hidden=await show_hidden_strings(request))
        except Exception as ex:
            raise sanic.exceptions.InvalidUsage("Specified resource cannot be obtained")
    return {
            'result': result,
            'is_last': True
    }


async def check_machine_owner(machine, request):
    if machine is None:
        raise sanic.exceptions.InvalidUsage("Specified resource cannot be obtained")
    if request.headers.get("AUTHORISED_AS", "None") == "admin":
        return
    if settings.app['service']['personalised'] and \
       machine.owner != request.headers["AUTHORISED_LOGIN"]:
        raise sanic.exceptions.InvalidUsage("Specified resource cannot be altered")


@machines.route('/machines/<machine_id>', methods=['DELETE'])
@el.log_func_boundaries
async def machine_delete(request, machine_id):
    el.log_d(request, "DELETE /machines, trying to obtain db session")

    with data.Connection.use() as conn:
        await asyncio.sleep(0.1)
        machine = data.Machine.get_one_for_update({'_id': machine_id}, conn=conn)
        await check_machine_owner(machine, request)
        new_request = data.Request(type=data.RequestType.UNDEPLOY, machine=str(machine_id))
        new_request.save(conn=conn)
        el.log_d(request, "new_request saved")
        machine.requests.append(new_request.id)
        machine.save(conn=conn)
        el.log_d(request, "machine saved")
        data.Action(type='other', request=new_request.id).save(conn=conn)
        el.log_d(request, "new_action saved")

    return {
            'request_id': '{}'.format(new_request.id),
            'is_last': False
    }


@machines.route('/machines/<machine_id>', methods=['PUT'])
async def machine_do_start_stop_reset(request, machine_id):
    logger.debug('Current thread name: {}'. format(threading.current_thread().name))

    action = request.headers.get('json_params').get('action')
    if action not in ['start', 'stop', 'restart']:
        raise sanic.exceptions.InvalidUsage('malformatted input json data, invalid or none \'action\' specified')

    request_type = data.RequestType(action)

    # do start / stop / reset
    with data.Connection.use() as conn:
        await asyncio.sleep(0.1)
        machine = data.Machine.get_one_for_update({'_id': machine_id}, conn=conn)
        # reset can be invoked only on running machine
        if request_type is data.RequestType.RESTART and machine.state is not data.MachineState.RUNNING:
            raise sanic.exceptions.InvalidUsage('Machine must be in state \'{}\' to invoke \'reset\', '
                                                'but was in state \'{}\'!'
                                                .format(data.MachineState.RUNNING, machine.state))

        await check_machine_owner(machine, request)
        new_request = data.Request(type=request_type, machine=str(machine_id))
        new_request.save(conn=conn)
        machine.requests.append(new_request.id)
        machine.save(conn=conn)
        data.Action(type='other', request=new_request.id).save(conn=conn)

    return {
            'request_id': '{}'.format(new_request.id),
            'is_last': False
    }
