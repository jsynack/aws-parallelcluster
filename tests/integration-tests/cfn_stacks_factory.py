# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import logging
from collections import OrderedDict

import boto3
from botocore.exceptions import ClientError
from framework.credential_providers import aws_credential_provider
from retrying import retry
from utils import retrieve_cfn_outputs, retrieve_cfn_resources


class CfnStack:
    """Identify a CloudFormation stack."""

    def __init__(self, name, region, template, parameters=None, capabilities=None):
        self.name = name
        self.region = region
        self.template = template
        self.parameters = parameters or []
        self.capabilities = capabilities or []
        self.cfn_stack_id = None
        self.__cfn_outputs = None
        self.__cfn_resources = None

    def init_stack_data(self):
        """Initialize cfn_outputs and cfn_resources."""
        self.__cfn_outputs = retrieve_cfn_outputs(self.name, self.region)
        self.__cfn_resources = retrieve_cfn_resources(self.name, self.region)

    @property
    def cfn_outputs(self):
        """
        Return the CloudFormation stack outputs for the stack.
        Outputs are retrieved only once and then cached.
        """
        if not self.__cfn_outputs:
            self.__cfn_outputs = retrieve_cfn_outputs(self.name, self.region)
        return self.__cfn_outputs

    @property
    def cfn_resources(self):
        """
        Return the CloudFormation stack resources for the stack.
        Resources are retrieved only once and then cached.
        """
        if not self.__cfn_resources:
            self.__cfn_resources = retrieve_cfn_resources(self.name, self.region)
        return self.__cfn_resources


class CfnStacksFactory:
    """Manage creation and deletion of CloudFormation stacks."""

    def __init__(self, credentials):
        self.__created_stacks = OrderedDict()
        self.__credentials = credentials

    def create_stack(self, stack):
        """
        Create a cfn stack with a given template.

        :param stack: stack to create.
        """
        name = stack.name
        region = stack.region

        id = self.__get_stack_internal_id(name, region)
        if id in self.__created_stacks:
            raise ValueError("Stack {0} already exists in region {1}".format(name, region))

        logging.info("Creating stack {0} in region {1}".format(name, region))
        is_template_url = stack.template.startswith("https://")
        with aws_credential_provider(region, self.__credentials):
            try:
                cfn_client = boto3.client("cloudformation", region_name=region)
                if is_template_url:
                    result = cfn_client.create_stack(
                        StackName=name,
                        TemplateURL=stack.template,
                        Parameters=stack.parameters,
                        Capabilities=stack.capabilities,
                    )
                else:
                    result = cfn_client.create_stack(
                        StackName=name,
                        TemplateBody=stack.template,
                        Parameters=stack.parameters,
                        Capabilities=stack.capabilities,
                    )
                stack.cfn_stack_id = result["StackId"]
                self.__created_stacks[id] = stack
                final_status = self.__wait_for_stack_creation(stack.cfn_stack_id, cfn_client)
                self.__assert_stack_status(final_status, "CREATE_COMPLETE")
                # Initialize the stack data while still in the credential context
                stack.init_stack_data()
            except Exception as e:
                logging.error("Creation of stack {0} in region {1} failed with exception: {2}".format(name, region, e))
                raise

        logging.info("Stack {0} created successfully in region {1}".format(name, region))

    @retry(
        stop_max_attempt_number=10,
        wait_fixed=5000,
        retry_on_exception=lambda exception: isinstance(exception, ClientError),
    )
    def delete_stack(self, name, region):
        """Destroy a created cfn stack."""
        with aws_credential_provider(region, self.__credentials):
            id = self.__get_stack_internal_id(name, region)
            if id in self.__created_stacks:
                logging.info("Destroying stack {0} in region {1}".format(name, region))
                try:
                    stack = self.__created_stacks[id]
                    cfn_client = boto3.client("cloudformation", region_name=stack.region)
                    cfn_client.delete_stack(StackName=stack.name)
                    final_status = self.__wait_for_stack_deletion(stack.cfn_stack_id, cfn_client)
                    self.__assert_stack_status(final_status, "DELETE_COMPLETE")
                except Exception as e:
                    logging.error(
                        "Deletion of stack {0} in region {1} failed with exception: {2}".format(name, region, e)
                    )
                    raise
                del self.__created_stacks[id]
                logging.info("Stack {0} deleted successfully in region {1}".format(name, region))
            else:
                logging.warning(
                    "Couldn't find stack with name {0} in region {1}. Skipping deletion.".format(name, region)
                )

    def delete_all_stacks(self):
        """Destroy all created stacks."""
        logging.debug("Destroying all cfn stacks")
        for value in reversed(OrderedDict(self.__created_stacks).values()):
            try:
                self.delete_stack(value.name, value.region)
            except Exception as e:
                logging.error(
                    "Failed when destroying stack {0} in region {1} with exception {2}.".format(
                        value.name, value.region, e
                    )
                )

    @retry(
        retry_on_result=lambda result: result == "CREATE_IN_PROGRESS",
        wait_fixed=5000,
        retry_on_exception=lambda exception: isinstance(exception, ClientError) and "Rate exceeded" in str(exception),
    )
    def __wait_for_stack_creation(self, name, cfn_client):
        return self.__get_stack_status(name, cfn_client)

    @retry(
        retry_on_result=lambda result: result == "DELETE_IN_PROGRESS",
        wait_fixed=5000,
        retry_on_exception=lambda exception: isinstance(exception, ClientError) and "Rate exceeded" in str(exception),
    )
    def __wait_for_stack_deletion(self, name, cfn_client):
        return self.__get_stack_status(name, cfn_client)

    @staticmethod
    def __get_stack_status(name, cfn_client):
        return cfn_client.describe_stacks(StackName=name).get("Stacks")[0].get("StackStatus")

    @staticmethod
    def __assert_stack_status(status, expected_status):
        if status != expected_status:
            raise Exception("Stack status {0} differs from expected one {1}".format(status, expected_status))

    @staticmethod
    def __get_stack_internal_id(name, region):
        return name + "-" + region
