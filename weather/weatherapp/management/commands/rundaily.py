"""
A custom django-admin command to collect emails for daily notifications.
This should be run as follows :
$python manage.py rundaily
"""

from django.core.management.base import BaseCommand, CommandError
from django.core.management import setup_environ
import settings
setup_environ(settings)

from weatherapp import emails
from weatherapp.models import *
from config import config

from datetime import *
#from onionoo_wrapper.objects import *
#from onionoo_wrapper.utilities import *
from onion_py.manager import *
from onion_py.objects import *


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TWO_MONTHS = 2 * 30 * 86400


global relays_details
global relays_uptime
global relays_bandwidth


def calculate_2mo_avg(relay, data_type):
    """ Calculates the average of values in 2-month time frame """
    # Check if required data is present in the relay object
    if data_type == 'uptime':
        if hasattr(relay, 'uptime') and '3_months' in relay.uptime:
            data = relay.uptime['3_months']
        else:
            return -1
    elif data_type == 'bandwidth':
        if hasattr(relay, 'write_history') and\
        '3_months' in relay.write_history:
            data = relay.write_history['3_months']
        else:
            return -1
    # Sum up all values within past 2 months
    _sum = 0
    count = 0
    today = datetime.now()
    first = datetime.strptime(data.first, "%Y-%m-%d %H:%M:%S")
    last = datetime.strptime(data.last, "%Y-%m-%d %H:%M:%S")
    for i in range(data.count):
        value_date = first + timedelta(seconds=(i * float(data.interval)))
        if (today - value_date).total_seconds() <= TWO_MONTHS:
            if data.values[i] not in [None, 'null']:
                _sum += (data.values[i])
                count += 1
    # Calculate the result
    if count == 0:
        return 0
    return (_sum * data.factor) / count


def get_uptime_percent(relay):
    """ Calculates the relay's uptime from onionoo's uptime document """
    return round(calculate_2mo_avg(relay, 'uptime') * 100, 2)


def get_avg_bandwidth(relay):
    """ Calculates relay's avg bandwidth from onionoo's bandwidth document """
    return round(calculate_2mo_avg(relay, 'bandwidth') / 1000.0, 2)


def get_cutoff_time():
    """ Returns the cutoff time for routers to be considered """
    return (datetime.now() - timedelta(days=6*30))


def get_deploy_time():
    """ Returns the time of deployment of current weather instance """
    deployed_query = DeployedDatetime.objects.all()
    if len(deployed_query) == 0:
        # DeployedDatetime model hasn't been populated yet
        deploy_time = datetime.now()
        DeployedDatetime(deployed=deploy_time).save()
    else:
        deploy_time = deployed_query[0].deployed
    return deploy_time


def get_relays(doc_type):
    """ Returns a list of relays from Onionoo as corresponding objects """
    req = Manager()
    params = {
        'type': 'relay',
        'running': 'true'
    }
    doc = req.query(doc_type, **params)
    return doc.relays


def add_router_entry(relay):
    """ Adds entry corresponding to the relay to the Router model """
    is_exit = checks.check_exitport(relay)
    router_entry = Router(fingerprint=relay.fingerprint,
                          name=relay.nickname,
                          welcomed=True,
                          last_seen=str(relay.last_seen),
                          up=True,
                          exit=is_exit)
    router_entry.save()


def delete_old_router_entries():
    """ Deletes relay entries with old enough timestamps """
    cutoff_time = get_cutoff_time()
    deploy_time = get_deploy_time()
    for entry in Router.objects.all():
        #last_seen = datetime.strptime(entry.last_seen, TIME_FORMAT)
        last_seen = entry.last_seen
        if (last_seen - max(deploy_time, cutoff_time)).total_seconds() < 0:
            entry.delete()


def is_recent(relay):
    """ Returns True if relay is recent enough, False otherwise """
    first_seen = datetime.strptime(relay.first_seen, TIME_FORMAT)
    cutoff_time = get_cutoff_time()
    most_recent = cutoff_time
    if config.check_deploy_time is True:
        deploy_time = get_deploy_time()
        most_recent = max(deploy_time, cutoff_time)
    time_diff = (first_seen - most_recent).total_seconds()
    return (time_diff > 0)


def check_first_seen(relay):
    """ Checks if relay was first seen at least 2 months ago """
    today = datetime.now()
    first_seen = datetime.strptime(relay.first_seen, TIME_FORMAT)
    return (today - first_seen).total_seconds() >= TWO_MONTHS


def check_tshirt_constraints(first_seen_check, exit_check, uptime, bandwidth):
    """ Returns True if T-shirt eligibility criteria are satisfied,
        False otherwise """
    if uptime == -1 or bandwidth == -1:
        raise DataError("Insufficient data")
    if first_seen_check is False or uptime < 95:
        return False
    else:
        if exit_check is False:
            if bandwidth >= 500:
                return True
            else:
                return False
        else:
            if bandwidth < 100:
                return False
            else:
                return True


def check_welcome(relay_tpl):
    """ Implements welcome script functionality and returns welcome email """
    relay = relay_tpl[0]
    if relay.is_stable() and is_recent(relay):
        matches = Router.objects.filter(fingerprint=relay.fingerprint)
        if not matches:
            # New relay so populate Router model and add to email list
            add_router_entry(relay)
            email_id = scraper.deobfuscate_mail(relay)
            if email_id != '':
                email = emails.welcome_tuple(email_id,
                                             relay.fingerprint,
                                             relay.nickname,
                                             checks.check_exitport(relay))
                return email


def check_tshirt(relay_tpl):
    """ Implements tshirt script functionality and returns tshirt email """
    relay = relay_tpl[1]
    first_seen = datetime.strptime(relay.first_seen, TIME_FORMAT)
    first_seen_check = check_first_seen(relay)
    exit_port_check = checks.check_exitport(relay)
    uptime_percent = get_uptime_percent(relay_tpl[1])
    avg_bandwidth = get_avg_bandwidth(relay_tpl[2])
    if check_tshirt_constraints(first_seen_check,
                                exit_port_check,
                                uptime_percent,
                                avg_bandwidth) is True:
        # Collect subscribers' emails
        subscriptions = TShirtSub.objects.filter(
            subscriber__router__fingerprint=relay.fingerprint,
            subscriber__confirmed=True, emailed=False)
        email = None
        if len(subscriptions) == 0:
            # No subscribers yet; Check and send email to operator only
            email_id = relay.parse_email()
            operator_sub = TShirtSub.objects.filter(
                subscriber__router__fingerprint=relay.fingerprint,
                subscriber__email=email_id, emailed=True)
            if len(operator_sub) > 0:
                # Relay operator already notified
                return
            else:
                # Collect operator's email
                email = emails.t_shirt_tuple(email_id,
                                             relay.fingerprint,
                                             relay.nickname,
                                             avg_bandwidth,
                                             hours_since(first_seen),
                                             checks.check_exitport(relay),
                                             "https://www.torproject.org",
                                             "https://www.torproject.org")
            # Find relay entry in the Router model
            matches = Router.objects.filter(fingerprint=relay.fingerprint)
            if not matches:
                relay.last_seen = datetime.now().strftime(TIME_FORMAT)
                add_router_entry(relay)
                router = Router.objects.get(fingerprint=relay.fingerprint)
            else:
                router = matches[0]
            # Add operator entry in the Subscriber model
            subscriber = Subscriber(email=email_id,
                                    router=router,
                                    confirmed=True)
            subscriber.save()
            # Add subscription entry for relay-operator in the TShirtSub model
            tshirt_sub = TShirtSub(subscriber=subscriber,
                                   emailed=True,
                                   triggered=True,
                                   avg_bandwidth=avg_bandwidth,
                                   last_changed=first_seen)
            tshirt_sub.save()
        else:
            for sub in subscriptions:
                email = emails.t_shirt_tuple(sub.subscriber.email,
                                             relay.fingerprint,
                                             relay.nickname,
                                             avg_bandwidth,
                                             hours_since(first_seen),
                                             exit_port_check,
                                             sub.subscriber.unsubs_auth,
                                             sub.subscriber.pref_auth)
                sub.emailed = True
    return email


class Command(BaseCommand):
    help = 'Clears the Router and subscription models'

    def handle(self, *args, **options):
        # Fetch relays data from Onionoo
        relays_details = get_relays('details')
        relays_uptime = get_relays('uptime')
        relays_bandwidth = get_relays('bandwidth')
        if not len(relays_details) == len(relays_uptime) == len(relays_bandwidth):
            raise DataError("Inconsistent Onionoo data")

        relays = zip(relays_details, relays_uptime, relays_bandwidth)

        welcome_mails = map(check_welcome, relays)
        tshirt_mails = map(check_tshirt, relays)

        email_list = list(welcome_mails) + list(tshirt_mails)

        # Accumulate emails to be sent
        #email_list = []
        #for relay_index in range(len(relays_details)):
        #    email_list = check_welcome(relay_index, email_list)
        #    email_list = check_tshirt(relay_index, email_list)

        # Send the emails to the selected operators/subscribers
        #send_mass_mail(tuple(email_list), fail_silently=False)

        # Delete old Router entries from database
        delete_old_router_entries()


