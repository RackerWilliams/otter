"""
Initial implementation of a version one launch_server_v1 config.

Ultimately this launch config will be responsible for:
0) Generating server name and injecting our AS metadata (TODO)
1) Starting a server
2) Executing a user defined deployment script (TODO)
3) Adding the server to a load balancer.
4) Configuring MaaS? (TODO)

The shape of this is nowhere near solidified, probably most of these
functions are actually private and many of the utilities will get
moved out of here.

Also no attempt is currently being made to define the public API for
initiating a launch_server job.
"""

import json
import itertools
from copy import deepcopy

from twisted.internet.defer import gatherResults, maybeDeferred

import treq

from otter.util.config import config_value
from otter.util.http import (append_segments, headers, check_success,
                             wrap_request_error)
from otter.util.hashkey import generate_server_name
from otter.util.deferredutils import retry_and_timeout
from otter.util.retry import (repeating_interval, transient_errors_except,
                              TransientRetryError)


class UnexpectedServerStatus(Exception):
    """
    An exception to be raised when a server is found in an unexpected state.
    """
    def __init__(self, server_id, status, expected_status):
        super(UnexpectedServerStatus, self).__init__(
            'Expected {server_id} to have {expected_status}, '
            'has {status}'.format(server_id=server_id,
                                  status=status,
                                  expected_status=expected_status)
        )
        self.server_id = server_id
        self.status = status
        self.expected_status = expected_status


def server_details(server_endpoint, auth_token, server_id):
    """
    Fetch the details of a server as specified by id.

    :param str server_endpoint: A str base URI probably from the service
        catalog.

    :param str auth_token: The auth token.
    :param str server_id: The opaque ID of a server.

    :return: A dict of the server details.
    """
    path = append_segments(server_endpoint, 'servers', server_id)
    d = treq.get(path, headers=headers(auth_token))
    d.addCallback(check_success, [200, 203])
    d.addErrback(wrap_request_error, path, 'server_details')
    return d.addCallback(treq.json_content)


def wait_for_active(log,
                    server_endpoint,
                    auth_token,
                    server_id,
                    interval=5,
                    timeout=3600,
                    clock=None):
    """
    Wait until the server specified by server_id's status is 'ACTIVE'

    :param log: A bound logger.
    :param str server_endpoint: Server endpoint URI.
    :param str auth_token: Keystone Auth token.
    :param str server_id: Opaque nova server id.
    :param int interval: Polling interval in seconds.  Default: 5.
    :param int timeout: timeout to poll for the server status in seconds.
        Default 3600 (1 hour)

    :return: Deferred that fires when the expected status has been seen.
    """
    log.msg("Checking instance status every {interval} seconds",
            interval=interval)

    if clock is None:  # pragma: no cover
        from twisted.internet import reactor
        clock = reactor

    start_time = clock.seconds()

    def poll():
        def check_status(server):
            status = server['server']['status']

            if status == 'ACTIVE':
                time_building = clock.seconds() - start_time
                log.msg(("Server changed from 'BUILD' to 'ACTIVE' within "
                         "{time_building} seconds"),
                        time_building=time_building)
                return server

            elif status != 'BUILD':
                raise UnexpectedServerStatus(
                    server_id,
                    status,
                    'ACTIVE')

            else:
                raise TransientRetryError()  # just poll again

        sd = server_details(server_endpoint, auth_token, server_id)
        sd.addCallback(check_status)
        return sd

    timeout_description = ("Waiting for server <{0}> to change from BUILD "
                           "state to ACTIVE state").format(server_id)

    return retry_and_timeout(
        poll, timeout,
        can_retry=transient_errors_except(UnexpectedServerStatus),
        next_interval=repeating_interval(interval),
        clock=clock,
        deferred_description=timeout_description)


def create_server(server_endpoint, auth_token, server_config):
    """
    Create a new server.

    :param str server_endpoint: Server endpoint URI.
    :param str auth_token: Keystone Auth Token.
    :param dict server_config: Nova server config.

    :return: Deferred that fires with the CreateServer response as a dict.
    """
    path = append_segments(server_endpoint, 'servers')
    d = treq.post(path, headers=headers(auth_token),
                  data=json.dumps({'server': server_config}))
    d.addCallback(check_success, [202])
    d.addErrback(wrap_request_error, path, 'server_create')
    return d.addCallback(treq.json_content)


def add_to_load_balancer(endpoint, auth_token, lb_config, ip_address, undo):
    """
    Add an IP addressed to a load balancer based on the lb_config.

    TODO: Handle load balancer node metadata.

    :param str endpoint: Load balancer endpoint URI.
    :param str auth_token: Keystone Auth Token.
    :param str lb_config: An lb_config dictionary.
    :param str ip_address: The IP Address of the node to add to the load
        balancer.
    :param IUndoStack undo: An IUndoStack to push any reversable operations onto.

    :return: Deferred that fires with the Add Node to load balancer response
        as a dict.
    """
    lb_id = lb_config['loadBalancerId']
    port = lb_config['port']
    path = append_segments(endpoint, 'loadbalancers', str(lb_id), 'nodes')

    d = treq.post(path, headers=headers(auth_token),
                  data=json.dumps({"nodes": [{"address": ip_address,
                                              "port": port,
                                              "condition": "ENABLED",
                                              "type": "PRIMARY"}]}))
    d.addCallback(check_success, [200, 202])
    d.addErrback(wrap_request_error, path, 'add')

    def when_done(result):
        undo.push(remove_from_load_balancer,
                  endpoint,
                  auth_token,
                  lb_id,
                  result['nodes'][0]['id'])
        return result

    return d.addCallback(treq.json_content).addCallback(when_done)


def add_to_load_balancers(log, region, service_catalog, auth_token, server_id, launch_config, undo):
    """
    Add the specified IP to mulitple load balancer based on the configs in
    lb_configs.

    :param str endpoint: Load balancer endpoint URI.
    :param str auth_token: Keystone Auth Token.
    :param list lb_configs: List of lb_config dictionaries.
    :param str ip_address: IP address of the node to add to the load balancer.
    :param IUndoStack undo: An IUndoStack to push any reversable operations onto.

    :return: Deferred that fires with a list of 2-tuples of loadBalancerId, and
        Add Node response.
    """
    lb_region = config_value('regionOverrides.cloudLoadBalancers') or region
    cloudLoadBalancers = config_value('cloudLoadBalancers')
    cloudServersOpenStack = config_value('cloudServersOpenStack')

    log.msg("Looking for load balancer endpoint",
            service_name=cloudLoadBalancers,
            region=lb_region)

    lb_endpoint = public_endpoint_url(service_catalog,
                                      cloudLoadBalancers,
                                      lb_region)

    log.msg("Looking for cloud servers endpoint",
            service_name=cloudServersOpenStack,
            region=region)

    server_endpoint = public_endpoint_url(service_catalog,
                                          cloudServersOpenStack,
                                          region)

    lb_config = launch_config.get('loadBalancers', [])

    d = server_details(server_endpoint, auth_token, server_id)

    def add_to_lbs(server):
        ip_address = private_ip_addresses(server)[0]
        return maybeDeferred(add_next, None, ip_address)

    return d.addCallback(add_to_lbs)

    lb_iter = iter(lb_configs)
    results = []

    def add_next(_, ip_address):
        try:
            lb_config = lb_iter.next()

            d = add_to_load_balancer(lb_endpoint, auth_token, lb_config, ip_address, undo)
            d.addCallback(lambda response, lb_id: (lb_id, response), lb_config['loadBalancerId'])
            d.addCallback(results.append)
            d.addCallback(add_next, ip_address)
            return d
        except StopIteration:
            return results


def endpoints(service_catalog, service_name, region):
    """
    Search a service catalog for matching endpoints.

    :param list service_catalog: List of services.
    :param str service_name: Name of service.  Example: 'cloudServersOpenStack'
    :param str region: Region of service.  Example: 'ORD'

    :return: Iterable of endpoints.
    """
    for service in service_catalog:
        if service_name != service['name']:
            continue

        for endpoint in service['endpoints']:
            if region != endpoint['region']:
                continue

            yield endpoint


def public_endpoint_url(service_catalog, service_name, region):
    """
    Return the first publicURL for a given service in a given region.

    :param list service_catalog: List of services.
    :param str service_name: Name of service.  Example: 'cloudServersOpenStack'
    :param str region: Region of service.  Example: 'ORD'

    :return: URL as a string.
    """
    return list(endpoints(service_catalog, service_name, region))[0]['publicURL']


def private_ip_addresses(server):
    """
    Get all private IPv4 addresses from the addresses section of a server.

    :param dict server: A server body.
    :return: List of IP addresses as strings.
    """
    return [addr['addr'] for addr in server['server']['addresses']['private']
            if addr['version'] == 4]


def prepare_launch_config(scaling_group_uuid, launch_config):
    """
    Prepare a launch_config for the specified scaling_group.

    This is responsible for returning a copy of the launch config that
    has metadata and unique server names added.

    :param IScalingGroup scaling_group: The scaling group this server is
        getting launched for.

    :param dict launch_config: The complete launch_config args we want to build
        servers from.

    :return dict: The prepared launch config.
    """
    launch_config = deepcopy(launch_config)
    server_config = launch_config['server']

    if 'metadata' not in server_config:
        server_config['metadata'] = {}

    server_config['metadata']['rax:auto_scaling_group_id'] = scaling_group_uuid

    if server_config.get('name'):
        server_name = server_config.get('name')
        server_config['name'] = '{0}-{1}'.format(server_name, generate_server_name())
    else:
        server_config['name'] = generate_server_name()

    for lb_config in launch_config.get('loadBalancers', []):
        if 'metadata' not in lb_config:
            lb_config['metadata'] = {}
        lb_config['metadata']['rax:auto_scaling_group_id'] = scaling_group_uuid
        lb_config['metadata']['rax:auto_scaling_server_name'] = server_config['name']

    return launch_config


def launch_server(log, region, scaling_group, service_catalog, auth_token,
                  launch_config, undo):
    """
    Launch a new server given the launch config auth tokens and service catalog.
    Possibly adding the newly launched server to a load balancer.

    :param BoundLog log: A bound logger.
    :param str region: A rackspace region as found in the service catalog.
    :param IScalingGroup scaling_group: The scaling group to add the launched
        server to.
    :param list service_catalog: A list of services as returned by the auth apis.
    :param str auth_token: The user's auth token.
    :param dict launch_config: A launch_config args structure as defined for
        the launch_server_v1 type.
    :param IUndoStack undo: The stack that will be rewound if undo fails.

    :return: Deferred that fires with a 2-tuple of server details and the
        list of load balancer responses from add_to_load_balancers.
    """
    launch_config = prepare_launch_config(scaling_group.uuid, launch_config)

    cloudLoadBalancers = config_value('cloudLoadBalancers')
    cloudServersOpenStack = config_value('cloudServersOpenStack')

    log.msg("Looking for cloud servers endpoint",
            service_name=cloudServersOpenStack,
            region=region)

    server_endpoint = public_endpoint_url(service_catalog,
                                          cloudServersOpenStack,
                                          region)

    server_config = launch_config['server']

    log = log.bind(server_name=server_config['name'])

    return create_server(server_endpoint, auth_token, server_config)


def remove_from_load_balancer(endpoint, auth_token, loadbalancer_id, node_id):
    """
    Remove a node from a load balancer.

    :param str endpoint: Load balancer endpoint URI.
    :param str auth_token: Keystone Auth Token.
    :param str loadbalancer_id: The ID for a cloud loadbalancer.
    :param str node_id: The ID for a node in that cloudloadbalancer.

    :returns: A Deferred that fires with None if the operation completed successfully,
        or errbacks with an APIError.
    """
    path = append_segments(endpoint, 'loadbalancers', str(loadbalancer_id), 'nodes', str(node_id))
    d = treq.delete(path, headers=headers(auth_token))
    d.addCallback(check_success, [200, 202])
    d.addErrback(wrap_request_error, path, 'remove')
    d.addCallback(lambda _: None)
    return d


def delete_server(log, region, service_catalog, auth_token, server_id, lb_info):
    """
    Delete the server specified by instance_details.

    TODO: Load balancer draining.

    :param str region: A rackspace region as found in the service catalog.
    :param list service_catalog: A list of services as returned by the auth apis.
    :param str auth_token: The user's auth token.
    :param tuple instance_details: A 2-tuple of server_id and a list of
        load balancer Add Node responses.

        Example::

        ('da08965f-4c2d-41aa-b492-a3c02706202f',
         [('12345',
           {'nodes': [{'id': 'a', 'address': ... }]}),
          ('54321',
           {'nodes': [{'id': 'b', 'address': ... }]})])

    :return: TODO
    """
    log.msg("Looking for cloud servers endpoint: %(service_name)s",
            service_name=cloudServersOpenStack,
            region=region)

    server_endpoint = public_endpoint_url(service_catalog,
                                          cloudServersOpenStack,
                                          region)

    if lb_info:
        d = remove_from_load_balancers(log, region, service_catalog, auth_token, lb_info)
        d.addCallback(lambda _: verified_delete(log, server_endpoint, auth_token, server_id))
        return d
    else:
        return verified_delete(log, server_endpoint, auth_token, server_id)


def remove_from_load_balancers(log, region, service_catalog, auth_token, lb_details):

    lb_region = config_value('regionOverrides.cloudLoadBalancers') or region
    cloudLoadBalancers = config_value('cloudLoadBalancers')
    cloudServersOpenStack = config_value('cloudServersOpenStack')

    log.msg("Looking for load balancer endpoint: %(service_name)s",
            service_name=cloudLoadBalancers,
            region=lb_region)

    lb_endpoint = public_endpoint_url(service_catalog,
                                      cloudLoadBalancers,
                                      lb_region)

    node_info = itertools.chain(
        *[[(loadbalancer_id, node['id']) for node in node_details['nodes']]
          for (loadbalancer_id, node_details) in lb_details])

    d = gatherResults(
        [remove_from_load_balancer(lb_endpoint, auth_token, loadbalancer_id, node_id)
         for (loadbalancer_id, node_id) in node_info], consumeErrors=True)

    return d


def verified_delete(log,
                    server_endpoint,
                    auth_token,
                    server_id):
    """
    Attempt to delete a server from the server endpoint, and ensure that it is
    deleted by trying again until getting the server results in a 404.

    There is a possibility Nova sometimes fails to delete servers.  Log if this
    happens, and if so, re-evaluate workarounds.

    Time out attempting to verify deletes after a period of time and log an
    error.

    :param log: A bound logger.
    :param str server_endpoint: Server endpoint URI.
    :param str auth_token: Keystone Auth token.
    :param str server_id: Opaque nova server id.
    :param int interval: Deletion interval in seconds - how long until
        verifying a delete is retried. Default: 5.
    :param int timeout: Seconds after which the deletion will be logged as a
        failure, if Nova fails to return a 404.  Default is 3660, because if
        the server is building, the delete will not happen until immediately
        after it has finished building.

    :return: Deferred that fires when the expected status has been seen.
    """
    del_log = log.bind(instance_id=server_id)
    del_log.msg('Deleting server')

    path = append_segments(server_endpoint, 'servers', server_id)
    d = treq.delete(path, headers=headers(auth_token))
    d.addCallback(check_success, [204])
    d.addErrback(wrap_request_error, path, 'server_delete')

    return d
