"""
Test to verify executing a webhook.
"""
from test_repo.autoscale.fixtures import ScalingGroupWebhookFixture


class ExecuteWebhook(ScalingGroupWebhookFixture):
    """
    Create a webhook, get and validate
    """

    def test_execute_webhook(self):
        """
        Execute a webhook and verify response code 202 and headers.
        """
        cap_url = self.webhook['links'].capability
        execute_wb_resp = self.autoscale_client.execute_webhook(cap_url)
        self.assertEquals(execute_wb_resp.status_code, 202,
                          msg='Execute webhook failed with {0} for group'
                          ' {1}'.format(execute_wb_resp.status_code, self.group.id))
        self.validate_headers(execute_wb_resp.headers)

    def test_execute_webhook_after_update(self):
        """
        Update a webhook and verify execution of the same.
        """
        cap_url_before_update = self.webhook['links'].capability
        update_webhook_resp = self.autoscale_client.update_webhook(group_id=self.group.id,
                                                                   policy_id=self.policy['id'],
                                                                   webhook_id=self.webhook['id'],
                                                                   name='update_execute_webhook',
                                                                   metadata={})
        self.assertEquals(update_webhook_resp.status_code, 204,
                          msg='Update webhook failed with {0} for group '
                          '{1}'.format(update_webhook_resp.status_code, self.group.id))
        updated_webhook_response = self.autoscale_client.get_webhook(self.group.id,
                                                                     self.policy['id'],
                                                                     self.webhook['id'])
        updated_webhook = updated_webhook_response.entity
        cap_url_after_update = updated_webhook.links.capability
        self.assertEquals(cap_url_before_update, cap_url_after_update,
                          msg='Capability URL changed upon update to webhook name for group '
                          '{0}'.format(self.group.id))
        execute_wb_resp = self.autoscale_client.execute_webhook(cap_url_after_update)
        self.assertEquals(execute_wb_resp.status_code, 202,
                          msg='Execute webhook failed with {0} for group '
                          '{1}'.format(execute_wb_resp.status_code, self.group.id))
        self.validate_headers(execute_wb_resp.headers)
        self.empty_scaling_group(self.group)
