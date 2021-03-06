"""
The OtterClock.  Because, while a broken clock is right twice a day, an OtterClock
is right all the time and is probably what caused your regular clock to get broken
in the first place.
"""

from datetime import datetime
from functools import partial
from croniter import croniter

from twisted.internet import defer
from twisted.application.internet import TimerService

from silverberg.lock import BasicLock, BusyLockError, with_lock

from otter.util.hashkey import generate_transaction_id
from otter.controller import maybe_execute_scaling_policy, CannotExecutePolicyError
from otter.log import log as otter_log
from otter.models.interface import NoSuchPolicyError, NoSuchScalingGroupError
from otter.util.deferredutils import ignore_and_log


def next_cron_occurrence(cron):
    """
    Return next occurence of given cron entry
    """
    return croniter(cron, start_time=datetime.utcnow()).get_next(ret_type=datetime)


class SchedulerService(TimerService):
    """
    Service to trigger scheduled events
    """

    def __init__(self, batchsize, interval, slv_client, store, clock=None):
        """
        Initializes the scheduler service with batch size and interval

        :param int batchsize: number of events to fetch on each iteration
        :param int interval: time between each iteration
        :param slv_client: a :class:`silverberg.client.CQLClient` or
                    :class:`silverberg.cluster.RoundRobinCassandraCluster` instance used to get lock
        :param clock: An instance of IReactorTime provider that defaults to reactor if not provided
        """
        from otter.models.cass import LOCK_TABLE_NAME
        self.lock_table = LOCK_TABLE_NAME
        self.slv_client = slv_client
        self.log = otter_log.bind(system='otter.scheduler')
        TimerService.__init__(self, interval, self.check_for_events, batchsize)
        self.store = store
        self.clock = clock

    def check_for_events(self, batchsize):
        """
        Check for events in the database before the present time.

        :return: a deferred that fires with None
        """

        def check_for_more(events):
            if events and len(events) == batchsize:
                return _do_check()
            return None

        def _do_check():
            lock_log = self.log.bind(category='locking')
            lock = BasicLock(self.slv_client, self.lock_table, 'schedule', max_retry=0,
                             log=lock_log)
            d = with_lock(lock, self.fetch_and_process, batchsize)
            d.addCallback(check_for_more)
            d.addErrback(ignore_and_log, BusyLockError, lock_log,
                         "Couldn't get lock to process events")
            d.addErrback(self.log.err)
            return d

        return _do_check()

    def fetch_and_process(self, batchsize):
        """
        Fetch the events to be processed and process them.
        Also delete/update after processing them

        :return: a deferred that fires with list of events processed
        """
        def process_events(events):

            if not len(events):
                return events, set()

            log.msg('Processing {num_events} events', num_events=len(events))

            deleted_policy_ids = set()

            deferreds = [
                self.execute_event(log, event, deleted_policy_ids)
                for event in events
            ]
            d = defer.gatherResults(deferreds, consumeErrors=True)
            return d.addCallback(lambda _: (events, deleted_policy_ids))

        def update_delete_events((events, deleted_policy_ids)):
            """
            Update events with cron entry with next trigger time
            Delete other events
            """
            if not len(events):
                return events

            events_to_delete, events_to_update = [], []
            for event in events:
                if event['cron'] and event['policyId'] not in deleted_policy_ids:
                    event['trigger'] = next_cron_occurrence(event['cron'])
                    events_to_update.append(event)
                else:
                    events_to_delete.append(event['policyId'])

            log.msg('Deleting {policy_ids_deleting} events',
                    policy_ids_deleting=len(events_to_delete))
            log.msg('Updating {policy_ids_updating} events',
                    policy_ids_updating=len(events_to_update))
            d = self.store.update_delete_events(events_to_delete, events_to_update)

            return d.addCallback(lambda _: events)

        # utcnow because of cass serialization issues
        utcnow = datetime.utcnow()
        log = self.log.bind(scheduler_run_id=generate_transaction_id(), utcnow=utcnow)
        deferred = self.store.fetch_batch_of_events(utcnow, batchsize)
        deferred.addCallback(process_events)
        deferred.addCallback(update_delete_events)
        deferred.addErrback(log.err)
        return deferred

    def execute_event(self, log, event, deleted_policy_ids):
        """
        Execute a single event

        :param log: A bound log for logging
        :param event: event dict to execute
        :param deleted_policy_ids: Set of policy ids that are deleted. Policy id will be added
                                   to this if its scaling group or policy has been deleted
        :return: a deferred with the results of execution
        """
        tenant_id, group_id, policy_id = event['tenantId'], event['groupId'], event['policyId']
        log = log.bind(tenant_id=tenant_id, scaling_group_id=group_id, policy_id=policy_id)
        log.msg('Executing policy')
        group = self.store.get_scaling_group(log, tenant_id, group_id)
        d = group.modify_state(partial(maybe_execute_scaling_policy,
                                       log, generate_transaction_id(),
                                       policy_id=policy_id))
        d.addErrback(ignore_and_log, CannotExecutePolicyError, log, 'Cannot execute policy')

        def collect_deleted_policy(failure):
            failure.trap(NoSuchScalingGroupError, NoSuchPolicyError)
            deleted_policy_ids.add(policy_id)

        d.addErrback(collect_deleted_policy)
        d.addErrback(log.err, 'Scheduler failed to execute policy')
        return d
