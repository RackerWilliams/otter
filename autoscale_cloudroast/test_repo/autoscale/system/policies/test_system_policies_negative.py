"""
System tests for scaling policies negative scenarios
"""
from test_repo.autoscale.fixtures import AutoscaleFixture


class ScalingPoliciesNegativeFixture(AutoscaleFixture):

    """
    System tests to verify execute scaling policies scenarios
    """

    @classmethod
    def setUpClass(cls):
        """
        Instantiate client and configs
        """
        super(ScalingPoliciesNegativeFixture, cls).setUpClass()

    def setUp(self):
        """
        Create a scaling group with minentities = maxentities, scale up by 2
        """
        self.create_group_response = self.autoscale_behaviors.create_scaling_group_given(
            gc_min_entities=2,
            gc_cooldown=0)
        self.group = self.create_group_response.entity
        self.policy_up_data = {'change': 2}
        self.policy_up = self.autoscale_behaviors.create_policy_webhook(
            group_id=self.group.id,
            policy_data=self.policy_up_data,
            execute_policy=False)
        self.policy_down_data = {'change': -2}
        self.policy_down = self.autoscale_behaviors.create_policy_webhook(
            group_id=self.group.id,
            policy_data=self.policy_down_data,
            execute_policy=False)
        self.resources.add(self.group.id,
                           self.autoscale_client.delete_scaling_group)

    def tearDown(self):
        """
        Delete scaling group
        """
        self.empty_scaling_group(self.group)

    def test_system_execute_policy_when_maxentities_equals_minentities(self):
        """
        Update minentities=maxentities and verify execution of a scale up policy
        """
        self._update_group_min_max_entities(group=self.group,
                                            maxentities=self.group.groupConfiguration.minEntities)
        execute_policy_up = self.autoscale_client.execute_policy(self.group.id,
                                                                 self.policy_up['policy_id'])
        self.assertEquals(execute_policy_up.status_code, 403,
                          msg='Scale up policy executed when minentities=maxentities: %s'
                          % execute_policy_up.status_code)

    def test_system_execute_scale_down_on_newly_created_group_with_minentities(self):
        """
        Update minentities=maxentities and verify execution of a scale down policy
        """
        self._update_group_min_max_entities(group=self.group,
                                            maxentities=self.group.groupConfiguration.minEntities)
        execute_policy_down = self.autoscale_client.execute_policy(
            self.group.id,
            self.policy_down['policy_id'])
        self.assertEquals(execute_policy_down.status_code, 403,
                          msg='Scale down policy executed when minentities=maxentities'
                          ' on the group %s with response code %s'
                          % (self.group.id, execute_policy_down.status_code))

    def test_system_delete_policy_during_execution(self):
        """
        Verify policy excution is not affected/paused when the policy is deleted during execution
        """
        execute_policy_up = self.autoscale_client.execute_policy(self.group.id,
                                                                 self.policy_up['policy_id'])
        self.assertEquals(execute_policy_up.status_code, 202,
                          msg='Scale up policy failed for group %s cause policy was deleted'
                          ' during execution: %s'
                          % (self.group.id, execute_policy_up.status_code))
        delete_policy = self.autoscale_client.delete_scaling_policy(
            self.group.id,
            self.policy_up['policy_id'])
        self.assertEquals(delete_policy.status_code, 204,
                          msg='Deleting the scaling policy while its executing failed %s'
                          % delete_policy.status_code)
        active_servers_list = self.autoscale_behaviors.wait_for_active_list_in_group_state(
            group_id=self.group.id,
            active_servers=self.group.groupConfiguration.minEntities + self.policy_up_data['change'])
        self.assertEquals(len(active_servers_list),
                          self.group.groupConfiguration.minEntities + self.policy_up_data['change'])

    def test_system_execute_scale_up_after_maxentities_met(self):
        """
        Update max entities of the scaling group to be 4 and execute scale up policy
        once to update active servers = maxentities successfully and verify reexecuting the
        policy when max entities are already met fails with 403
        """
        upd_maxentities = 3
        self._update_group_min_max_entities(group=self.group,
                                            maxentities=upd_maxentities)
        change_num = upd_maxentities - \
            self.group.groupConfiguration.minEntities
        policy_up = {'change': change_num, 'cooldown': 0}
        execute_policy = self.autoscale_behaviors.create_policy_webhook(
            group_id=self.group.id,
            policy_data=policy_up,
            execute_policy=True)
        self.assertEquals(execute_policy['execute_response'], 202,
                          msg='Scale up policy execution failed for group %s'
                          'when change delta < maxentities with response: %s'
                          % (self.group.id, execute_policy['execute_response']))
        reexecute_scale_up = self.autoscale_client.execute_policy(
            self.group.id,
            execute_policy['policy_id'])
        self.assertEquals(reexecute_scale_up.status_code, 403,
                          msg='Scale up policy executed for group %s when group already'
                          ' has maxentities, response code: %s'
                          % (self.group.id, reexecute_scale_up.status_code))

    def test_system_scaleup_update_min_max_0_delete_group(self):
        """
        Create a scaling group and execute a scale up policy, update min and max entities
        to be 0 and delete the group and verify all the on the group servers are deleted
        before the user can delete the group
        """
        execute_policy_up = self.autoscale_client.execute_policy(self.group.id,
                                                                 self.policy_up['policy_id'])
        self.assertEquals(execute_policy_up.status_code, 202,
                          msg='Scale up policy execution failed for group %s '
                          'when change delta < maxentities with response: %s'
                          % (self.group.id, execute_policy_up.status_code))
        self._update_group_min_max_entities(group=self.group,
                                            maxentities=0, minentities=0)
        delete_group = self.autoscale_client.delete_scaling_group(
            self.group.id)
        self.assertEquals(delete_group.status_code, 204,
                          msg='Delete group failed for group %s when min and maxentities '
                          'is update to 0 with response %s'
                          % (self.group.id, delete_group.status_code))
        # find a way to list servers in a scaling group without using the group state call
        # i.e. using nova to verify the servers were deleted

    def test_system_scaleup_update_min_scale_down(self):
        """
        Create a scaling group and execute a scale up policy, update min = current desired capacity
        execute a scale down policy and verify its unsuccessful.
        """
        execute_policy_up = self.autoscale_client.execute_policy(self.group.id,
                                                                 self.policy_up['policy_id'])
        self.assertEquals(execute_policy_up.status_code, 202,
                          msg='Scale up policy execution failed for group %s '
                          'when change delta < maxentities with response: %s'
                          % (self.group.id, execute_policy_up.status_code))
        self._update_group_min_max_entities(group=self.group,
                                            minentities=self.group.groupConfiguration.minEntities +
                                            self.policy_up_data['change'])
        execute_policy_down = self.autoscale_client.execute_policy(
            self.group.id,
            self.policy_down['policy_id'])
        self.assertEquals(execute_policy_down.status_code, 403,
                          msg='Scale down policy executed when minentities=maxentities'
                          ' on the group %s with response code %s'
                          % (self.group.id, execute_policy_down.status_code))

    def _update_group_min_max_entities(self, group, maxentities=None, minentities=None):
        """
        Updates the scaling groups maxentities to the given and asserts the update
        was successful
        """
        if minentities is None:
            minentities = group.groupConfiguration.minEntities
        if maxentities is None:
            maxentities = group.groupConfiguration.maxEntities
        update_group = self.autoscale_client.update_group_config(
            group_id=group.id,
            name=group.groupConfiguration.name,
            cooldown=group.groupConfiguration.cooldown,
            min_entities=minentities,
            max_entities=maxentities,
            metadata={})
        self.assertEquals(update_group.status_code, 204,
                          msg='Updating minentities and/or maxentities in the group config'
                          ' for %s failed: %s'
                          % (group.id, update_group.status_code))
