# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The unique worker functionality for this service is contained here.

The entry-point is the **cb_subscription_handler**

The design and flow leverage a few constraints that are placed upon it
by NATS Streaming and using AWAIT on the default loop.
- NATS streaming queues require one message to be processed at a time.
- AWAIT on the default loop effectively runs synchronously

If these constraints change, the use of Flask-SQLAlchemy would need to change.
Flask-SQLAlchemy currently allows the base model to be changed, or reworking
the model to a standalone SQLAlchemy usage with an async engine would need
to be pursued.
"""
import json
import os

import nats
from auth_api.models import db
from auth_api.services.rest_service import RestService
from auth_api.utils.roles import ADMIN, COORDINATOR
from entity_queue_common.service import QueueServiceManager
from entity_queue_common.service_utils import QueueException, logger
from flask import Flask  # pylint: disable=wrong-import-order

from account_mailer import config  # pylint: disable=wrong-import-order
from account_mailer.auth_utils import get_member_emails
from account_mailer.email_processors import common_mailer  # pylint: disable=wrong-import-order
from account_mailer.email_processors import pad_confirmation  # pylint: disable=wrong-import-order
from account_mailer.email_processors import payment_completed  # pylint: disable=wrong-import-order
from account_mailer.email_processors import refund_requested  # pylint: disable=wrong-import-order
from account_mailer.enums import MessageType, SubjectType, TemplateType
from account_mailer.services import notification_service  # pylint: disable=wrong-import-order


qsm = QueueServiceManager()  # pylint: disable=invalid-name
APP_CONFIG = config.get_named_config(os.getenv('DEPLOYMENT_ENV', 'production'))
FLASK_APP = Flask(__name__)
FLASK_APP.config.from_object(APP_CONFIG)
db.init_app(FLASK_APP)


# pylint: disable=too-many-statements, too-many-branches
async def process_event(event_message: dict, flask_app):
    """Process the incoming queue event message."""
    if not flask_app:
        raise QueueException('Flask App not available.')

    with flask_app.app_context():
        message_type = event_message.get('type', None)
        email_msg = None
        email_dict = None
        token = RestService.get_service_account_token()
        logger.debug('message_type recieved %s', message_type)
        if message_type == 'account.mailer':
            email_msg = json.loads(event_message.get('data'))
            email_dict = payment_completed.process(email_msg)
        elif message_type == MessageType.REFUND_REQUEST.value:
            email_msg = event_message.get('data')
            email_dict = refund_requested.process(email_msg)
        elif message_type == MessageType.PAD_ACCOUNT_CREATE.value:
            email_msg = event_message.get('data')
            email_dict = pad_confirmation.process(email_msg, token)
        elif message_type == MessageType.NSF_LOCK_ACCOUNT.value:
            email_msg = event_message.get('data')
            logger.debug('lock account message recieved:')
            template_name = TemplateType.NSF_LOCK_ACCOUNT_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN, COORDINATOR))
            subject = SubjectType.NSF_LOCK_ACCOUNT_SUBJECT.value
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject)
        elif message_type == MessageType.NSF_UNLOCK_ACCOUNT.value:
            email_msg = event_message.get('data')
            logger.debug('unlock account message recieved')
            template_name = TemplateType.NSF_UNLOCK_ACCOUNT_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN, COORDINATOR))
            subject = SubjectType.NSF_UNLOCK_ACCOUNT_SUBJECT.value
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject)
        elif message_type == MessageType.ACCOUNT_CONFIRMATION_PERIOD_OVER.value:
            email_msg = event_message.get('data')
            template_name = TemplateType.ACCOUNT_CONF_OVER_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            nsf_fee = email_msg.get('nsfFee')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN,))
            subject = SubjectType.ACCOUNT_CONF_OVER_SUBJECT.value
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject,
                                               nsf_fee=nsf_fee)
        elif message_type in (MessageType.TEAM_MODIFIED.value, MessageType.TEAM_MEMBER_INVITED.value):
            email_msg = event_message.get('data')
            logger.debug('Team Modified message recieved')
            template_name = TemplateType.TEAM_MODIFIED_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN,))
            subject = SubjectType.TEAM_MODIFIED_SUBJECT.value
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject)
        elif message_type == MessageType.ADMIN_REMOVED.value:
            email_msg = event_message.get('data')
            logger.debug('ADMIN_REMOVED message recieved')
            template_name = TemplateType.ADMIN_REMOVED_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            recipient_email = email_msg.get('recipientEmail')
            subject = SubjectType.ADMIN_REMOVED_SUBJECT.value
            email_dict = common_mailer.process(org_id, recipient_email, template_name, subject)
        elif message_type == MessageType.PAD_INVOICE_CREATED.value:
            email_msg = event_message.get('data')
            template_name = TemplateType.PAD_INVOICE_CREATED_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN,))
            subject = SubjectType.PAD_INVOICE_CREATED.value
            args = {
                'nsf_fee': email_msg.get('nsfFee'),
                'invoice_total': email_msg.get('invoice_total'),
            }
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject,
                                               **args)
        elif message_type in (MessageType.ONLINE_BANKING_OVER_PAYMENT.value,
                              MessageType.ONLINE_BANKING_UNDER_PAYMENT.value, MessageType.ONLINE_BANKING_PAYMENT.value):
            email_msg = event_message.get('data')

            if message_type == MessageType.ONLINE_BANKING_OVER_PAYMENT.value:
                template_name = TemplateType.ONLINE_BANKING_OVER_PAYMENT_TEMPLATE_NAME.value
            elif message_type == MessageType.ONLINE_BANKING_UNDER_PAYMENT.value:
                template_name = TemplateType.ONLINE_BANKING_UNDER_PAYMENT_TEMPLATE_NAME.value
            else:
                template_name = TemplateType.ONLINE_BANKING_PAYMENT_TEMPLATE_NAME.value

            org_id = email_msg.get('accountId')
            admin_emails = get_member_emails(org_id, (ADMIN,))
            subject = SubjectType.ONLINE_BANKING_PAYMENT_SUBJECT.value
            args = {
                'title': subject,
                'paid_amount': email_msg.get('amount'),
                'credit_amount': email_msg.get('creditAmount'),
            }
            email_dict = common_mailer.process(org_id, admin_emails, template_name, subject, **args)
        elif message_type == MessageType.PAD_SETUP_FAILED.value:
            email_msg = event_message.get('data')
            template_name = TemplateType.PAD_SETUP_FAILED_TEMPLATE_NAME.value
            org_id = email_msg.get('accountId')
            admin_coordinator_emails = get_member_emails(org_id, (ADMIN,))
            subject = SubjectType.PAD_SETUP_FAILED.value
            args = {
                'accountId': email_msg.get('accountId'),
            }
            email_dict = common_mailer.process(org_id, admin_coordinator_emails, template_name, subject,
                                               **args)
        if email_dict:
            logger.debug('Extracted email msg Recipient: %s ', email_dict.get('recipients', ''))
            process_email(email_dict, FLASK_APP, token)
        else:
            # TODO probably an unnhandled event.handle better
            logger.error('No email content generate----------------------')


def process_email(email_dict: dict, flask_app: Flask, token: str):  # pylint: disable=too-many-branches
    """Process the email contained in the message."""
    if not flask_app:
        raise QueueException('Flask App not available.')

    with flask_app.app_context():
        logger.debug('Attempting to process email: %s', email_dict.get('recipients', ''))
        # get type from email
        notification_service.send_email(email_dict, token=token)


async def cb_subscription_handler(msg: nats.aio.client.Msg):
    """Use Callback to process Queue Msg objects."""
    event_message = None
    try:
        logger.info('Received raw message seq:%s, data=  %s', msg.sequence, msg.data.decode())
        event_message = json.loads(msg.data.decode('utf-8'))
        logger.debug('Event Message Received: %s', event_message)
        await process_event(event_message, FLASK_APP)
    except Exception:  # NOQA # pylint: disable=broad-except
        # Catch Exception so that any error is still caught and the message is removed from the queue
        logger.error('Queue Error: %s', json.dumps(event_message), exc_info=True)
