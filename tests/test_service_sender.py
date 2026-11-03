"""Services and newsletters stay out of the people list, and real people stay in it."""
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.services import service_sender as ss
from api.services.entity_resolver import EntityResolver
from api.services.gmail import METADATA_HEADERS, EmailMessage, GmailService, is_bulk_mail
from api.services.gmail_skip_cache import GmailSkipCache
from api.services.google_auth import GoogleAccount
from api.services.interaction_store import InteractionStore
from api.services.link_override import LinkOverrideStore
from api.services.person_entity import PersonEntity, PersonEntityStore, is_hidden_service
from api.services.person_facts import PersonFactStore
from api.services.relationship import RelationshipStore
from api.services.relationship_metrics import compute_all_dunbar_circles
from api.services.service_sender import SenderEvidence, address_shape, classify_sender, name_signals
from api.services.source_entity import SourceEntity, SourceEntityStore
from scripts import repair_service_senders as repair
from scripts import sync_consistency_verify as consistency

pytestmark = pytest.mark.unit

SERVICE_NAME = "Acme Bank"
SERVICE_ADDRESS = "service@acmewidgets.com"
OWN_ADDRESS = "jane@janes-letter.com"
NOW = "2026-06-15T00:00:00+00:00"
T = ss.ONE_WAY_MIN_RECEIVED


def verdict(name, address, received=1, **kwargs):
    addresses = address if isinstance(address, dict) else {address: max(received, 1)}
    return classify_sender(SenderEvidence(name, addresses, received=received, **kwargs))


class TestServiceAddressesGoWhateverTheName:
    @pytest.mark.parametrize("name,address,rule", [
        ("Acme", "no-reply@acmewidgets.com", "automated_address"),
        ("Jane Sample", "noreply@acmewidgets.com", "automated_address"),
        ("Acme Online", "olbnoreply@acmewidgets.com", "automated_address"),
        ("Jane Sample", "do.not.reply@acmewidgets.com", "automated_address"),
        ("Acme", "acme-newsletter@acmewidgets.com", "automated_address"),
        ("Acme", "mailer@acmewidgets.com", "automated_address"),
        ("Book Club", "book-club@googlegroups.com", "automated_address"),
        ("Sandbox Two", "sandbox-two@example.com", "automated_address"),
        ("Sandbox", "sandbox@mailer.test", "automated_address"),
        ("Acme Cafe", "messages+a1b2c3d4e5f6@paymentsplatform.com", "generated_address"),
        ("Acme", "4815162342@paymentsplatform.com", "generated_address"),
        ("Pat Lee", "desk@sendgrid.net", "mail_infrastructure"),
        ("Acme", "acme@e.acmewidgets.com", "sending_host"),
        ("Acme", "hello@em7.acmewidgets.com", "sending_host"),
        ("Jane Sample", "events@mail.acmewidgets.com", "sending_host"),
        ("Acme", "acme@acmewidgetsmail.com", "sending_host"),
        ("Acme", "buy@mynotifications.acmewidgets.com", "sending_host"),
        ("Sam from Acme", "sam@app.acmewidgets.com", "sending_host"),
        ("Service", "service@acmewidgets.com", "role_address"),
        (SERVICE_NAME, SERVICE_ADDRESS, "role_address"),
        ("Jane from Acme", "events@acmewidgets.com", "role_address"),
        ("Sam @ Acme", "events@acmewidgets.com", "role_address"),
        ("Acme® Card", "acmesupport@acmewidgets.com", "role_address"),
        ("Acmewidgets", "anything@acmewidgets.com", "named_after_domain"),
        ("Acme Widgets", "acme.widgets@acme.com", "named_after_domain"),
        ("Northwind Quick", "northwind@eq.northwind.com", "role_address"),
        ("Acme", "acme@acme.shipping-sample.com", "role_address"),
        ("Acme Receipts", "acme.france@acme.com", "named_after_domain"),
        ("Acme.com", "orders2@acmewidgets.com", "role_address"),
        ("Acme Alert", "acmealert@acme.com", "named_after_domain"),
        ("Acme", "acme@emessaging.acmewidgets.com", "sending_host"),
        ("Jane Sample", "support@acmewidgets.com", "role_address"),
        ("Winona Sample (Support)", "support@acmewidgets.com", "role_address"),
        ("Jane Sample via Platform", "pleasereply+thread-123456789@platformname.com", "generated_address"),
        ("Jane Sample via Platform", "platform@platformname.com", "relay_address"),
        ("Jane Sample via Platform", "desk@platformname.com", "relay_address"),
    ])
    def test_one_message_is_enough(self, name, address, rule):
        result = verdict(name, address)
        assert result.is_service, result
        assert result.reason == rule

    @pytest.mark.parametrize("address", [
        "accounting@acmewidgets.com", "customersupport@acmewidgets.com", "core-services@acmewidgets.com",
        "community@acmewidgets.com", "marketingoffers@acmewidgets.com",
    ])
    def test_replies_and_threads_do_not_save_a_role_mailbox(self, address):
        result = verdict("Pat Lee", address, 2, thread=355, sent_to={address: 78})
        assert result.is_service
        assert not result.wrote_to_own_address

    def test_a_reply_does_not_save_an_organization_address(self):
        for name, address in (
            ("Sms Compliance Operations", "sms_compliance_operations@northwindtrading.com"),
            ("Ent Credit Control Inc", "credit-control-inc@northwindtrading.com"),
        ):
            result = verdict(name, address, 7, thread=4, sent_to={address: 4})
            assert result.is_service, name
            assert not result.wrote_to_own_address, name


class TestTheOneWayRule:
    @pytest.mark.parametrize("name,address", [
        ("Jane Sample", "jane@janes-letter.com"),
        ("Theodora Sample", "theod@northwindtrading.com"),
        ("Jane Sample", "jsample@northwindtrading.com"),
        ("Acme Sales", "jane.sample@northwindtrading.com"),
        ("Jane Sample", "jane@jane-sample.com"),
        ("Jane Sample", "jane.sample@gmail.com"),
        ("Service", "someone@gmail.com"),
    ])
    def test_steady_mail_nobody_answered_is_a_service_on_any_own_address(self, name, address):
        below = verdict(name, address, T - 1)
        assert not below.is_service, below
        result = verdict(name, address, T)
        assert result.is_service and result.reason == "one_way_volume"
        assert ss.SIGNAL_ONE_WAY in result.signals

    @pytest.mark.parametrize("extra", [{"thread": 1}, {"sent_to": {"jane@janes-letter.com": 1}}])
    def test_one_thread_or_one_reply_keeps_an_own_address(self, extra):
        assert not verdict("Jane Sample", OWN_ADDRESS, 500, **extra).is_service

    def test_a_second_own_mailbox_does_not_rescue_a_row_nobody_answered(self):
        addresses = {"jane@janes-letter.com": 289, "jane.sample@gmail.com": 2}
        assert verdict("Jane Sample", addresses, 291).reason == "one_way_volume"

    def test_the_threshold_itself(self):
        assert not verdict("Jane Sample", OWN_ADDRESS, 9).is_service
        assert verdict("Jane Sample", OWN_ADDRESS, 10).is_service

    def test_an_address_with_no_name_in_it_takes_the_same_volume(self):
        assert not verdict("Pat Lee", "desk@northwindtrading.com", T - 1).is_service
        result = verdict("Pat Lee", "desk@northwindtrading.com", T)
        assert result.reason == "one_way_volume"
        assert ss.SIGNAL_ONE_WAY in result.signals

    def test_a_colleague_turns_up_in_threads(self):
        assert verdict("Pat Lee", "desk@northwindtrading.com", 100, thread=10).is_service
        assert not verdict("Pat Lee", "desk@northwindtrading.com", 100, thread=11).is_service

    def test_a_correspondent_gets_answers(self):
        address = "desk@northwindtrading.com"
        assert verdict("Pat Lee", address, 100, sent_to={address: 5}).is_service
        assert not verdict("Pat Lee", address, 100, sent_to={address: 6}).is_service


class TestAPersonsOwnAddress:
    @pytest.mark.parametrize("name,address", [
        ("Jane Sample", "jane.sample@gmail.com"),
        ("Service", "someone@gmail.com"),
        ("Jane Sample", "janesample@yahoo.co.uk"),
        ("Jane Sample", "janesample@outlook.de"),
        ("Jane Sample", "jane@hushmail.com"),
        ("Jane Sample", "sample@rediffmail.com"),
        ("Jane Sample", "anything@mailbox.org"),
        ("Jane Sample", "jane@acmewidgets.com"),
        ("Jane Sample", "jsample@acmewidgets.com"),
        ("Theodora Sample", "theod@acmewidgets.com"),
        ("Acme Sales", "jane.sample@acmewidgets.com"),
        ("Jane Sample", "jane@jane-sample.com"),
        ("Jane Sample", "jane@janesample.com"),
        ("Jane Sample", "founder@sample.com"),
        ("Jane Sample", "me@janesample.com"),
        ("Jane Sample", "j@janesample.com"),
        ("Jane Sample", "jsample@mail.stateuniversity.edu"),
        ("Sample, Jane", "jane.sample@mail.agency.gov"),
        ("Sample, Jane", "jane.sample@units.ac.uk"),
        ("Robin", "robin@acmewidgets.com"),
        ("Sample, Jane", "jane.sample@northwindtrading.com"),
        ("Jane Sample", "jane@mail.acmewidgets.com"),
        ("Pat Lee", "desk@northwindtrading.com"),
        ("Jane Mailer", "jane.mailer@northwindtrading.com"),
        ("Jane Mailer", "mailer@gmail.com"),
        ("Sarah Church", "sarah@stmarys.org"),
    ])
    def test_one_message_is_not_enough(self, name, address):
        result = verdict(name, address)
        assert not result.is_service, result

    def test_own_addresses_are_named_on_the_verdict(self):
        addresses = {"jane.sample@gmail.com": 3, "events@acmewidgets.com": 3}
        assert verdict("Jane Sample", addresses, 6).own_addresses == ("jane.sample@gmail.com",)

    def test_a_greeting_mailbox_on_their_own_site_takes_history(self):
        address = "hello@janesample.com"
        assert verdict("Jane Sample", address).is_service
        assert verdict("Jane Sample", address, 3, sent_to={address: 1}).is_service
        kept = verdict("Jane Sample", address, 3, sent_to={address: 2})
        assert not kept.is_service and kept.wrote_to_own_address
        assert not verdict("Jane Sample", address, 3, thread=2).is_service
        assert verdict("Jane Sample", address, 3, thread=1).is_service

    @pytest.mark.parametrize("address", ["support@blueapron.com", "team@blueapron.com", "info@blueapron.com"])
    def test_a_role_mailbox_on_a_company_named_like_a_person_never_counts(self, address):
        result = verdict("Blue Apron", address, 5, sent_to={address: 9}, thread=9)
        assert result.is_service
        assert not result.wrote_to_own_address

    @pytest.mark.parametrize("name,address", [
        ("Seavey Vineyard", "wine@seaveyvineyard.com"),
        ("Netroots Nation", "panels@netrootsnation.org"),
        ("Seen by Indeed", "talent@beseen.indeed.com"),
        ("Travis Ci", "builds@travis-ci.com"),
        ("Colorado Coffee Merchants", "stephanie@coloradocoffeemerchants.com"),
        ("Graining Fork Nature Preserve", "mngr@grainingfork.org"),
    ])
    def test_a_descriptive_local_part_on_a_name_matching_domain_is_a_company(self, name, address):
        result = verdict(name, address)
        assert result.is_service, result
        assert not result.own_addresses

    @pytest.mark.parametrize("address", ["sr@janesample.com", "j@janesample.com", "me@janesample.com", "founder@sample.com"])
    def test_a_personal_local_part_on_their_own_site(self, address):
        name = "Steven Rosson" if address.startswith("sr") else "Jane Sample"
        address = address.replace("janesample", "stevenrosson") if address.startswith("sr") else address
        assert verdict(name, address).own_addresses == (address,)

    def test_history_makes_any_local_part_on_their_own_site_theirs(self):
        assert verdict("Jane Sample", "desk@janesample.com", 4, thread=16).own_addresses
        assert not verdict("Jane Sample", "desk@janesample.com", 4).own_addresses

    def test_a_companys_name_is_not_a_site_even_with_replies(self):
        for name, address in (("Acme Bank", "hello@acmebank.com"), ("Dana (Acme)", "hello@acme.com")):
            assert verdict(name, address).is_service
            result = verdict(name, address, 3, sent_to={address: 2})
            assert result.is_service, name
            assert not result.wrote_to_own_address, name

    def test_their_site_by_a_hyphenated_name_or_by_surname(self):
        assert verdict("Jane Sample", "me@jane-sample.com", 3).reason == "own_address"
        assert verdict("Jane Sample", "founder@sample.com", 3).reason == "own_address"

    def test_a_shortened_name_counts_as_theirs_when_written_to(self):
        address = "theod@northwindtrading.com"
        assert verdict("Theodora Sample", address, 1, sent_to={address: 1}).wrote_to_own_address

    def test_a_one_word_brand_on_its_own_domain_is_not_personal(self):
        assert verdict("Northwindco", "northwindco@northwind.com").reason == "named_after_domain"

    def test_one_word_name_on_its_own_address(self):
        assert verdict("Robin", "robin@northwindtrading.com", 4).reason == "own_address"
        assert verdict("Etickets", "etickets@northwindtrading.com", 40).reason == "one_way_volume"
        assert verdict("Etickets", "etickets@northwindtrading.com", 5).reason == "not_a_person_name"
        address = "robin@northwindtrading.com"
        talking = verdict("Robin", address, 51, thread=18, sent_to={address: 30})
        assert talking.own_addresses == (address,) and talking.wrote_to_own_address

    def test_a_person_in_threads_on_their_own_domain_is_not_a_brand(self):
        assert not verdict("Jane Sample", "her@janesample.org", 0, thread=13).is_service

    def test_first_dot_last_on_the_company_name_is_not_a_person(self):
        assert not verdict("Jane Sample", "jane.sample@sample.com").is_service
        assert "role_address" in address_shape("acme@acme.com").signals
        assert "role_address" in address_shape("acme+tag@mail.acme.com").signals
        assert "role_address" not in address_shape("jane@acme.com").signals

    def test_a_big_mailbox_is_not_a_provider_just_for_its_first_label(self):
        assert not address_shape("jane@email.acmewidgets.com").personal_provider
        assert not address_shape("jane@mail.acmewidgets.com").personal_provider
        assert address_shape("jane@mail.com").personal_provider


class TestAReplyCounts:
    @pytest.mark.parametrize("name,address,received,replies", [
        ("Robert Sample", "bob@northwind.com", 20, 1),
        ("Pat Lee", "cq@northwind.com", 40, 2),
    ])
    def test_an_address_without_their_name(self, name, address, received, replies):
        result = verdict(name, address, received, sent_to={address: replies})
        assert result.wrote_to_own_address

    @pytest.mark.parametrize("address", ["support@northwind.com", "noreply@northwind.com", "a1b2c3d4e5f6@northwind.com"])
    def test_not_a_role_automated_or_generated_address(self, address):
        assert not verdict("Pat Lee", address, 40, sent_to={address: 5}).wrote_to_own_address

    def test_not_one_reply_lost_in_a_mailing(self):
        address = "schooltalk@northwind.com"
        assert verdict("Pat Lee", address, 100, sent_to={address: 1}).wrote_to_own_address
        assert not verdict("Pat Lee", address, 101, sent_to={address: 1}).wrote_to_own_address

    @pytest.mark.parametrize("name,address", [
        ("Acme Bank", "ar@northwind.com"),
        ("Pat (Support)", "ar@northwind.com"),
        ("Northwind Finance", "ar@northwind.com"),
        ("Sam from Acme", "ar@acme.com"),
        ("Acme® Card", "ar@northwind.com"),
    ])
    def test_not_a_company_name_on_an_address_without_a_person(self, name, address):
        assert not verdict(name, address, 11, sent_to={address: 4}).wrote_to_own_address

    def test_sustained_replies_beat_a_sending_host(self):
        address = "bm@mail.eastwind.com"
        assert verdict("Pat Lee", address, 30, sent_to={address: 30}).wrote_to_own_address
        assert verdict("Pat Lee", address, 30, sent_to={address: 3}).wrote_to_own_address
        assert not verdict("Pat Lee", address, 30, sent_to={address: 2}).wrote_to_own_address

    def test_not_a_true_sending_host(self):
        address = "bm@mail.eastwind.com"
        assert not verdict("Pat Lee", address, 30, sent_to={address: 30}, bulk=3, checked=3).wrote_to_own_address
        own = "pat@mail.eastwind.com"
        assert not verdict("Pat Lee", own, 30, sent_to={own: 30}, bulk=3, checked=3).wrote_to_own_address
        generated = "a1b2c3d4e5f6@mail.eastwind.com"
        assert not verdict("Pat Lee", generated, 30, sent_to={generated: 30}).wrote_to_own_address

    def test_bulk_mail_does_not_undo_a_reply_to_an_ordinary_address(self):
        assert verdict("Jane Sample", OWN_ADDRESS, 30, sent_to={OWN_ADDRESS: 1}, bulk=3, checked=3).wrote_to_own_address


class TestAListWithAStrayAddress:
    def test_a_list_host_outweighs_an_address_naming_no_one(self):
        addresses = {"progressive-exchange@googlegroups.com": 2, "discuss@progressiveexchange.org": 1}
        assert verdict("Progressive Exchange", addresses, 0, thread=3).reason == "automated_address"

    def test_an_address_naming_someone_keeps_them_in_play(self):
        addresses = {"newsletter@northwindtrading.com": 6, "jane@janes-letter.com": 4}
        assert verdict("Jane Sample", addresses, 9).reason == "own_address"


class TestRealHistoryOutweighsAddressShape:
    def test_a_sending_host_with_threads(self):
        address = "jane.sample@mail.northwind.com"
        assert not verdict("Jane Sample", address, 3, thread=200).is_service
        assert verdict("Jane Sample", address, 3).reason == "sending_host"

    def test_a_sending_host_with_replies(self):
        address = "jane@mail.northwind.com"
        kept = verdict("Jane Sample", address, 3, sent_to={address: 3})
        assert not kept.is_service and kept.wrote_to_own_address

    def test_two_messages_on_a_sending_host_are_enough_and_one_is_not(self):
        address = "jane@mail.northwind.com"
        assert verdict("Jane Sample", address, 2).is_service
        assert not verdict("Jane Sample", address, 1).is_service

    def test_a_company_name_on_a_sending_host_needs_no_run_of_mail(self):
        assert verdict("Acme Bank", "acme@mail.northwind.com").is_service
        assert verdict("Acme Widgets Monthly", "acme@mail.acmewidgets.com").is_service
        assert verdict("Acme® Card", "acme@mail.northwind.com").is_service

    @pytest.mark.parametrize("address", ["sarah@stmarys.org", "schurch@stmarys.org", "sarahchurch@stmarys.org"])
    def test_a_name_that_reads_as_an_organization_with_history(self, address):
        assert not verdict("Sarah Church", address, 3, thread=50).is_service

    def test_a_name_that_reads_as_an_organization_on_their_first_name(self):
        assert verdict("Sarah Church", "sarah@stmarys.org", 3).is_service
        assert not verdict("Sarah Church", "sarah@stmarys.org", 1).is_service
        assert verdict("Stoko Support", "stoko@elsewhere-sample.com", 2).is_service

    def test_history_only_helps_an_address_carrying_the_name(self):
        assert verdict("Sarah Church", "desk@stmarys.org", 3, thread=50).is_service
        replied = verdict("Sarah Church", "schurch@stmarys.org", 3, sent_to={"schurch@stmarys.org": 1})
        assert replied.own_addresses and replied.wrote_to_own_address

    def test_a_name_that_reads_as_an_organization_without_history(self):
        assert verdict("Acme Labs", "hq@northwindtrading.com", 2).reason == "organization_name"
        assert not verdict("Acme Labs", "hq@northwindtrading.com", 1).is_service
        assert verdict("Acme Club", "acmeclub@northwindtrading.com", 2).is_service

    def test_an_automated_address_is_never_own_whatever_it_spells(self):
        mixed = {"bounce@northwindtrading.com": 4, "desk@northwindtrading.com": 5}
        assert verdict("Pat Bounce", mixed, 9).reason == "no_service_evidence"

    def test_a_generated_address_is_never_own_whatever_it_spells(self):
        assert verdict("Jane Sample", "jane+a1b2c3d4e5f6@paymentsplatform.com").reason == "generated_address"


class TestJudgedPerAddress:
    def test_a_newsletter_merged_into_a_persons_row_does_not_sink_them(self):
        addresses = {"newsletter@northwindtrading.com": 6, "jane.sample@gmail.com": 4}
        result = verdict("Jane Sample", addresses, T - 1)
        assert not result.is_service
        assert result.own_addresses == ("jane.sample@gmail.com",)

    def test_one_own_address_at_a_small_share_still_counts(self):
        addresses = {"events@northwindtrading.com": 17, "jane.sample@northwindtrading.com": 3}
        assert verdict("Jane Sample", addresses, 9).reason == "own_address"

    def test_a_real_address_among_others(self):
        mostly_role = {"events@northwindtrading.com": 8, "jane.sample@northwindtrading.com": 2}
        assert not verdict("Jane Sample", mostly_role, T - 1).is_service
        barely = {"events@northwindtrading.com": 19, "jane.sample@northwindtrading.com": 1}
        assert verdict("Jane Sample", barely, 9).is_service

    def test_service_addresses_must_carry_half_the_mail(self):
        assert not verdict("Pat Lee", {"desk@northwindtrading.com": 5, "events@northwindtrading.com": 4}, 9).is_service
        assert verdict("Pat Lee", {"desk@northwindtrading.com": 4, "events@northwindtrading.com": 5}, 9).is_service

    def test_mixed_kinds_of_service_address_add_up(self):
        mixed = {"desk@northwindtrading.com": 4, "events@northwindtrading.com": 3, "noreply@acmewidgets.com": 3}
        assert verdict("Pat Lee", mixed, 9).reason == "mixed_service_addresses"

    def test_no_address_is_no_verdict(self):
        assert classify_sender(SenderEvidence("Acme", {}, received=3)).reason == "no_address"

    def test_a_reply_to_an_own_address_counts_even_with_a_service_address_too(self):
        addresses = {"jane.sample@gmail.com": 2, "events@acmewidgets.com": 8}
        result = verdict("Jane Sample", addresses, 10, sent_to={"jane.sample@gmail.com": 1})
        assert result.wrote_to_own_address

    def test_a_reply_to_the_service_address_alone_does_not(self):
        addresses = {"jane.sample@gmail.com": 2, "events@acmewidgets.com": 8}
        assert not verdict("Jane Sample", addresses, 10, sent_to={"events@acmewidgets.com": 1}).wrote_to_own_address

    def test_a_sent_message_with_no_recorded_address_counts_for_a_row_with_an_own_one(self):
        assert verdict("Jane Sample", OWN_ADDRESS, 1, sent_to={"": 1}).wrote_to_own_address
        assert not verdict(SERVICE_NAME, SERVICE_ADDRESS, 1, sent_to={"": 1}).wrote_to_own_address


class TestBulkMail:
    @pytest.mark.parametrize("received,bulk,checked,expected", [
        (8, 3, 3, True), (8, 2, 3, True), (8, 1, 3, False), (1, 1, 1, True), (2, 2, 2, True),
        (8, 0, 3, False), (8, 0, 0, False),
        # One bulk message says nothing about eight ordinary ones.
        (9, 1, 1, False), (9, 2, 2, False), (9, 3, 9, False), (9, 6, 9, True),
    ])
    def test_a_share_of_the_mail_checked(self, received, bulk, checked, expected):
        result = verdict("Jane Sample", "jane@janes-letter.com", received, bulk=bulk, checked=checked)
        assert result.is_service is expected
        assert (ss.SIGNAL_BULK_MAIL in result.signals) is expected

    def test_one_bulk_message_does_not_outweigh_a_correspondent(self):
        result = verdict("Jane Sample", OWN_ADDRESS, 32, sent_to={OWN_ADDRESS: 1}, bulk=1, checked=1)
        assert not result.is_service

    def test_someone_who_turns_up_in_threads(self):
        assert not verdict("Jane Sample", "jane.sample@gmail.com", 1, thread=59, bulk=1, checked=1).is_service
        assert verdict("Jane Sample", OWN_ADDRESS, 10, thread=1, bulk=3, checked=3).is_service
        assert not verdict("Jane Sample", OWN_ADDRESS, 10, thread=2, bulk=3, checked=3).is_service

    def test_someone_who_also_writes_from_their_own_mailbox(self):
        mixed = {"jane@janes-letter.com": 6, "jane.sample@gmail.com": 3}
        assert not verdict("Jane Sample", mixed, 9, bulk=3, checked=3).is_service
        barely = {"jane@janes-letter.com": 39, "jane.sample@gmail.com": 1}
        assert verdict("Jane Sample", barely, 9, bulk=3, checked=3).is_service

    def test_a_newsletter_sent_from_a_mailbox_alone_is_still_bulk(self):
        assert verdict("Jane Sample", "jane.sample@gmail.com", 8, bulk=3, checked=3).is_service

    @pytest.mark.parametrize("headers,expected", [
        ({"list-unsubscribe": "<mailto:x>"}, True),
        ({"precedence": "bulk"}, True),
        ({"precedence": " Junk "}, True),
        ({"precedence": "list"}, False),
        ({"list-unsubscribe": "<mailto:x>", "list-post": "<mailto:y>"}, False),
        ({"precedence": "bulk", "list-post": "<mailto:y>"}, False),
        ({}, False),
    ])
    def test_is_bulk_mail(self, headers, expected):
        assert is_bulk_mail(headers) is expected

    def _message(self, *headers):
        return {
            "id": "m1", "threadId": "t1", "snippet": "s", "labelIds": [],
            "payload": {"headers": [
                {"name": "From", "value": "Jane Sample <jane@acmewidgets.com>"},
                {"name": "Date", "value": "Mon, 15 Jun 2026 10:00:00 +0000"},
                *headers,
            ]},
        }

    def _service(self):
        service = GmailService.__new__(GmailService)
        service.account_type = GoogleAccount.PERSONAL
        return service

    def test_a_parsed_message_knows_it_is_bulk(self):
        service = self._service()
        bulk = self._message({"name": "List-Unsubscribe", "value": "<mailto:x>"})
        assert service._parse_message(bulk).bulk is True
        assert service._parse_message(self._message()).bulk is False

    def test_the_flag_stays_out_of_the_api_contract(self):
        assert "bulk" not in self._service()._parse_message(self._message()).to_dict()

    def test_batch_fetch_requests_the_headers_the_flag_reads(self):
        for header in ("List-Unsubscribe", "List-Post", "Precedence"):
            assert header in METADATA_HEADERS
        service = self._service()
        client = MagicMock()
        with patch.object(GmailService, "service", client), patch("api.services.gmail.time.sleep"):
            service.get_messages_batch(["m1"], include_body=False)
        client.users().messages().get.assert_any_call(
            userId="me", id="m1", format="metadata", metadataHeaders=METADATA_HEADERS,
        )

    def test_a_single_fetch_requests_the_headers_too(self):
        service = self._service()
        client = MagicMock()
        with patch.object(GmailService, "service", client), patch.object(GmailService, "_rate_limit"):
            service.get_message("m1", include_body=False)
        client.users().messages().get.assert_any_call(
            userId="me", id="m1", format="metadata", metadataHeaders=METADATA_HEADERS,
        )


class TestSignals:
    def test_name_signals(self):
        assert ss.SIGNAL_ONE_WORD in name_signals("Instagramish")
        assert ss.SIGNAL_ORG_NAME in name_signals("Northwind Bank")
        assert ss.SIGNAL_ORG_NAME in name_signals("The Northwind")
        assert ss.SIGNAL_ORG_NAME in name_signals("Northwind Team")
        assert ss.SIGNAL_ORG_NAME not in name_signals("The")
        assert ss.SIGNAL_ORG_NAME not in name_signals("Jane Sample")
        assert ss.SIGNAL_ORG_NAME in name_signals("Northwind.com Sample")
        assert ss.SIGNAL_ORG_NAME not in name_signals("J.R. Sample")
        assert ss.SIGNAL_NOT_A_NAME in name_signals("Acme® Card")
        assert ss.SIGNAL_NOT_A_NAME in name_signals("")
        assert ss.SIGNAL_NOT_A_NAME not in name_signals("Sam @ Acme")
        assert ss.SIGNAL_PERSONA in name_signals("Dana (Acme)")
        assert ss.SIGNAL_PERSONA in name_signals("Sam, Sales & Support")
        assert ss.SIGNAL_PERSONA not in name_signals("Sample, Jane")
        assert ss.SIGNAL_BRAND_NAME in name_signals("Acme Widgets", ["e.acmewidgets.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Northwind Monthly", ["northwind.com"])
        assert ss.SIGNAL_BRAND_NAME not in name_signals("Jane Sample", ["northwind.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Zzzclub", ["email.zzz-club.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Ledgerly", ["em1.ledgerly.northwind.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Abc Bank", ["abc.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Monthly Northwind", ["northwind.com"])
        assert ss.SIGNAL_BRAND_NAME in name_signals("Acme Widgets Monthly", ["acmewidgets.com"])
        assert ss.SIGNAL_BRAND_NAME not in name_signals("Abel Sample", ["ab.com"])

    def test_address_shapes(self):
        assert address_shape("<id@acmewidgets.com>").domain == "acmewidgets.com"
        assert address_shape("a@b.co.uk").signals == ()
        assert ss.SIGNAL_SENDING_HOST in address_shape("a@e.b.co.uk").signals
        assert ss.SIGNAL_SENDING_HOST not in address_shape("a@mail.b.edu").signals
        assert ss.SIGNAL_SENDING_HOST not in address_shape("a@mail.b.ac.uk").signals
        assert ss.SIGNAL_SENDING_HOST not in address_shape("a@us.b.com").signals
        assert ss.SIGNAL_ROLE in address_shape("customer_support@b.com").signals
        assert ss.SIGNAL_ROLE in address_shape("supportdesk@b.com").signals
        assert ss.SIGNAL_ROLE in address_shape("info15@b.com").signals
        assert ss.SIGNAL_ROLE not in address_shape("janet@b.com").signals
        assert ss.SIGNAL_MAIL_BRANDED in address_shape("a@email-b.com").signals
        assert ss.SIGNAL_GENERATED not in address_shape("jane.sample1980@b.com").signals
        assert ss.SIGNAL_GENERATED not in address_shape("jsample1980@b.com").signals
        assert ss.SIGNAL_GENERATED not in address_shape("abcde12345@b.com").signals
        assert ss.SIGNAL_GENERATED in address_shape("ab1cdefghijkl@b.com").signals
        assert ss.SIGNAL_GENERATED in address_shape("1ab23cdefghi@b.com").signals
        assert ss.SIGNAL_GENERATED in address_shape("a1b2c3d4e5@b.com").signals
        assert ss.SIGNAL_GENERATED in address_shape("0123456789abcdef0123456789abcdef@b.com").signals
        assert ss.SIGNAL_RESERVED in address_shape("a@sub.example.org").signals
        assert ss.SIGNAL_LIST_HOST in address_shape("a@sub.groups.io").signals
        # A provider's address has no other shape worth reading.
        assert address_shape("support@gmail.com").signals == ()

    def test_brand_rules(self):
        assert verdict("Bank Acme", "hq@acme.com").reason == "named_after_domain"
        assert verdict("Sam from Acme", "desk@acme.com").reason == "named_after_domain"
        assert verdict("Acme Widgets Monthly", "desk@acmewidgets.com").reason == "named_after_domain"
        assert not verdict("Acmewidgets", "anything@acmewidgets.com", 0, thread=3).is_service

    def test_names_that_are_not_a_person(self):
        assert verdict("Rachel & Chris", "desk@northwindtrading.com", 2).reason == "not_a_person_name"
        assert verdict("Etickets", "desk@northwindtrading.com", 2).reason == "not_a_person_name"
        assert not verdict("Bonnie", "desk@northwindtrading.com", 5, thread=3).is_service
        assert not verdict("", "pat@northwindtrading.com", 3).is_service

    def test_automated_words(self):
        assert ss.SIGNAL_AUTOMATED in address_shape("mailer@b.com").signals
        assert ss.SIGNAL_AUTOMATED not in address_shape("jane.mailer@b.com").signals
        assert ss.SIGNAL_AUTOMATED not in address_shape("mailer@gmail.com").signals
        assert ss.SIGNAL_AUTOMATED in address_shape("noreply@gmail.com").signals

    def test_regional_providers(self):
        assert address_shape("a@hotmail.fr").personal_provider
        assert address_shape("a@yahoo.co.uk").personal_provider
        assert not address_shape("a@yahoo.info").personal_provider
        assert not address_shape("a@yahoo.northwind.uk").personal_provider
        for provider in ("hushmail.com", "rediffmail.com", "mailbox.org"):
            assert address_shape(f"a@{provider}").personal_provider, provider

    def test_name_in_the_address(self):
        assert not ss._carries_name(address_shape("desk+jane@b.com"), "Jane Sample")
        assert not ss._carries_name(address_shape("delivery@b.com"), "Jo Li")
        assert ss._carries_name(address_shape("lee@b.com"), "Jo Lee")


class World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.crm_db = tmp_path / "crm.db"
        self.interactions_db = tmp_path / "interactions.db"
        self.merged = tmp_path / "merged_person_ids.json"
        self.merged.write_text("{}")
        self.people = PersonEntityStore(str(self.crm_db))
        self.sources = SourceEntityStore(str(self.crm_db))
        RelationshipStore(str(self.crm_db))
        LinkOverrideStore(self.crm_db)
        PersonFactStore(str(self.crm_db))
        InteractionStore(str(self.interactions_db))
        self.execute_sql(
            "CREATE TABLE IF NOT EXISTS entity_review_queue (id TEXT PRIMARY KEY, review_type TEXT, "
            "person_a_id TEXT, person_b_id TEXT, status TEXT, created_at TEXT)"
        )
        self.review = tmp_path / "private" / "review.md"

    def person(self, name=SERVICE_NAME, emails=None, relationship_strength=None, **fields):
        entity = PersonEntity(
            canonical_name=name, display_name=name,
            emails=[SERVICE_ADDRESS] if emails is None else emails,
            **fields,
        )
        if relationship_strength is not None:
            entity.relationship_strength = relationship_strength
        return self.people.add(entity)

    def interaction(self, person, source="gmail", title="← Subject", source_id=None, account="personal",
                    when=NOW, attendees=None):
        conn = sqlite3.connect(self.interactions_db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, source_id, source_account, "
            "attendee_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), person.id, when, source, title, source_id or uuid.uuid4().hex, account, attendees),
        )
        conn.commit()
        conn.close()

    def source_entity(self, person, source="gmail", source_id=None, email=SERVICE_ADDRESS, status="auto", metadata=None):
        return self.sources.add(SourceEntity(
            source_type=source,
            source_id=source_id or uuid.uuid4().hex,
            observed_name=person.canonical_name,
            observed_email=email,
            canonical_person_id=person.id,
            link_status=status,
            metadata=metadata or {},
        ), validate_person=False)

    def mail(self, person, count=6, address=SERVICE_ADDRESS, metadata=None):
        for _ in range(count):
            source_id = uuid.uuid4().hex
            self.interaction(person, source_id=source_id)
            self.source_entity(person, source_id=source_id, email=address, metadata=metadata)

    def service(self, **fields):
        person = self.person(**fields)
        self.mail(person)
        return person

    def newsletter(self, name="Jane Sample", address=OWN_ADDRESS, count=T):
        person = self.person(name=name, emails=[address])
        self.mail(person, count=count, address=address)
        return person

    def execute_sql(self, sql, *params, db=None):
        conn = sqlite3.connect(db or self.crm_db)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def decisions(self, samples=None):
        return ss.decide_senders(self.crm_db, self.interactions_db, self.merged, samples)

    def decision(self, person, samples=None):
        return next(d for d in self.decisions(samples) if d.person.id == person.id)

    def hide(self, person, store=None):
        ss.hide_service_person(store or self.people, self.decision(person))

    def interactions(self):
        conn = sqlite3.connect(self.interactions_db)
        rows = conn.execute("SELECT person_id, title FROM interactions").fetchall()
        conn.close()
        return rows

    def args(self, *extra):
        return [
            "--crm-db", str(self.crm_db), "--interactions-db", str(self.interactions_db),
            "--merged-ids", str(self.merged), "--report", str(self.review), *extra,
        ]

    def run(self, *extra):
        assert repair.main(self.args(*extra)) == 0
        return PersonEntityStore(str(self.crm_db))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class TestStillProtected:
    def test_baseline_service_is_hidden(self, world):
        """The control for every case below: this row goes unless something saves it."""
        assert world.decision(world.service()).will_hide

    def _assert_protected(self, world, person, reason):
        decision = world.decision(person)
        assert decision.verdict.is_service, "the row must look like a service for this to prove anything"
        assert reason in decision.protected_by
        assert not decision.will_hide
        repair.execute_decisions(world.decisions(), world.crm_db)
        assert not world.people.get_by_id(person.id).hidden

    @pytest.mark.parametrize("source", ["imessage", "whatsapp", "phone", "photos"])
    def test_one_to_one_channels(self, world, source):
        person = world.service()
        world.interaction(person, source=source, title="Hello")
        self._assert_protected(world, person, f"interaction:{source}")

    def test_a_slack_dm(self, world):
        person = world.service()
        world.interaction(person, source="slack", title="Slack DM with Sam")
        self._assert_protected(world, person, "interaction:slack")

    def test_a_slack_channel_is_not_one_to_one(self, world):
        person = world.service()
        world.interaction(person, source="slack", title="Message in a channel")
        assert world.decision(person).will_hide

    def test_a_whatsapp_group_is_not_one_to_one(self, world):
        person = world.service()
        world.interaction(person, source="whatsapp", title="WhatsApp group", attendees=30)
        assert world.decision(person).will_hide

    @pytest.mark.parametrize("source", ["imessage", "phone", "photos", "linkedin"])
    def test_identity_source_entities(self, world, source):
        person = world.service()
        world.source_entity(person, source=source, email=None)
        self._assert_protected(world, person, f"source_entity:{source}")

    @pytest.mark.parametrize("field,value,reason", [
        ("phone_numbers", ["+15555550100"], "phone_number"),
        ("photo_count", 2, "photos"),
        ("tags", ["friend"], "tags"),
        ("notes", "Met at a synthetic conference", "notes"),
        ("birthday", "01-02", "birthday"),
        ("linkedin_url", "https://linkedin.example/in/sample", "linkedin_url"),
        ("company", "Sample Co", "company"),
        ("position", "Sample Title", "position"),
        ("sources", ["manual_fix"], "manual_source"),
        ("category", "family", "family"),
        ("category", "self", "family"),
    ])
    def test_fields_on_the_row(self, world, field, value, reason):
        person = world.service(**{field: value})
        self._assert_protected(world, person, reason)

    def test_automatic_sources_protect_nothing(self, world):
        person = world.service(sources=["gmail", "vault", "meeting", "granola", "calendar", "contacts"])
        assert world.decision(person).will_hide

    @pytest.mark.parametrize("column", ["preferred_person_id", "rejected_person_id"])
    def test_link_overrides(self, world, column):
        person = world.service()
        other = "other-person"
        preferred, rejected = (person.id, other) if column == "preferred_person_id" else (other, person.id)
        world.execute_sql(
            "INSERT INTO link_overrides (id, name_pattern, source_type, context_pattern, preferred_person_id, "
            "rejected_person_id, reason, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
            "o1", "acme", "x", preferred, rejected, "human", NOW,
        )
        self._assert_protected(world, person, "link_override")

    def _fact(self, world, person, confirmed):
        world.execute_sql(
            "INSERT INTO person_facts (id, person_id, category, key, value, confidence, extracted_at, "
            "confirmed_by_user, created_at) VALUES (?, ?, 'work', 'k', 'v', 1.0, ?, ?, ?)",
            uuid.uuid4().hex, person.id, NOW, confirmed, NOW,
        )

    def test_a_confirmed_fact(self, world):
        person = world.service()
        self._fact(world, person, 1)
        self._assert_protected(world, person, "confirmed_fact")

    def test_an_unconfirmed_fact_is_not_his_word(self, world):
        person = world.service()
        self._fact(world, person, 0)
        assert world.decision(person).will_hide

    @pytest.mark.parametrize("registry,reason", [
        ("STRENGTH_OVERRIDES_BY_ID", "strength_override"),
        ("CIRCLE_OVERRIDES_BY_ID", "circle_override"),
        ("TAG_OVERRIDES_BY_ID", "tag_override"),
    ])
    def test_manual_overrides(self, world, monkeypatch, registry, reason):
        person = world.service()
        monkeypatch.setattr(ss, registry, {person.id: 1})
        self._assert_protected(world, person, reason)

    def test_the_owner(self, world, monkeypatch):
        person = world.service()
        monkeypatch.setattr(ss.settings, "my_person_id", person.id, raising=False)
        self._assert_protected(world, person, "self")

    def test_family_by_id(self, world, monkeypatch):
        person = world.service()
        monkeypatch.setattr(ss, "FAMILY_PERSON_IDS", {person.id})
        self._assert_protected(world, person, "family")

    def test_family_by_name(self, world, monkeypatch):
        person = world.service()
        monkeypatch.setattr(ss, "_is_family_member", lambda name: name == person.canonical_name)
        self._assert_protected(world, person, "family")

    @pytest.mark.parametrize("kind,protected", [("friend", True), ("inferred", False), ("coworker", False)])
    def test_relationships(self, world, kind, protected):
        person = world.service()
        world.execute_sql(
            "INSERT INTO relationships (id, person_a_id, person_b_id, relationship_type, created_at, updated_at) "
            "VALUES (?, ?, 'other', ?, ?, ?)",
            "r1", person.id, kind, NOW, NOW,
        )
        decision = world.decision(person)
        assert decision.will_hide is not protected
        assert (f"relationship:{kind}" in decision.protected_by) is protected

    def test_relationships_protect_either_side(self, world):
        person = world.service()
        world.execute_sql(
            "INSERT INTO relationships (id, person_a_id, person_b_id, relationship_type, created_at, updated_at) "
            "VALUES (?, 'other', ?, 'friend', ?, ?)",
            "r1", person.id, NOW, NOW,
        )
        assert not world.decision(person).will_hide

    @pytest.mark.parametrize("column", ["person_a_id", "person_b_id"])
    def test_a_review_marked_kept(self, world, column):
        person = world.service()
        world.execute_sql(
            f"INSERT INTO entity_review_queue (id, review_type, {column}, status, created_at) "
            "VALUES (?, 'merge', ?, 'kept', ?)",
            "q1", person.id, NOW,
        )
        self._assert_protected(world, person, "review_kept")

    def test_a_pending_review_protects_nothing(self, world):
        person = world.service()
        world.execute_sql(
            "INSERT INTO entity_review_queue (id, review_type, person_a_id, status, created_at) "
            "VALUES ('q1', 'merge', ?, 'pending', ?)",
            person.id, NOW,
        )
        assert world.decision(person).will_hide

    def test_a_merge_target(self, world):
        person = world.service()
        world.merged.write_text(json.dumps({"merged-away": person.id}))
        self._assert_protected(world, person, "merge_target")

    def test_a_merged_away_row_is_not_judged(self, world):
        person = world.service()
        world.merged.write_text(json.dumps({person.id: "survivor"}))
        assert person.id not in {d.person.id for d in world.decisions()}


class TestWhatNoLongerVouches:
    def test_writing_to_a_role_mailbox(self, world):
        person = world.service()
        world.interaction(person, title="→ Hello", source_id=f"abc:{SERVICE_ADDRESS}")
        world.source_entity(person, source_id=f"abd:{SERVICE_ADDRESS}")
        decision = world.decision(person)
        assert decision.sent == 1
        assert decision.will_hide

    def test_writing_to_a_role_mailbox_is_counted_from_either_record(self, world):
        person = world.service()
        world.source_entity(person, source_id=f"abd:{SERVICE_ADDRESS}")
        assert world.decision(person).sent == 1

    @pytest.mark.parametrize("source", ["calendar", "contacts", "phone_contacts", "vault", "meeting", "whatsapp", "slack"])
    def test_thin_source_entities(self, world, source):
        person = world.service()
        world.source_entity(person, source=source, email=None)
        assert world.decision(person).will_hide

    @pytest.mark.parametrize("source,attendees", [("calendar", 3), ("calendar", 40), ("meeting", None), ("vault", None)])
    def test_invites_and_mentions_on_a_service_address(self, world, source, attendees):
        person = world.service()
        world.interaction(person, source=source, title="Event", attendees=attendees)
        assert world.decision(person).will_hide

    def test_a_confirmed_link_on_a_service_address(self, world):
        person = world.service()
        world.source_entity(person, status="confirmed")
        assert world.decision(person).will_hide

    def test_a_rejected_link(self, world):
        person = world.service()
        world.source_entity(person, status="rejected")
        assert world.decision(person).will_hide

    def test_notes_on_a_person_shaped_name_at_a_marketing_address(self, world):
        person = world.person(name="Jane Sample", emails=["events@mail.acmewidgets.com"])
        world.mail(person, address="events@mail.acmewidgets.com")
        world.interaction(person, source="vault", title="Note")
        world.source_entity(person, source="vault", email=None)
        assert world.decision(person).will_hide


class TestOwnAddressProtection:
    def _one_way(self, world):
        person = world.newsletter()
        assert world.decision(person).will_hide
        return person

    @pytest.mark.parametrize("title,source_id", [
        ("→ Hello", f"abc:{OWN_ADDRESS}"),
        ("→ Hello", "plainid"),
        ("Hello", f"abc:{OWN_ADDRESS}"),
    ])
    def test_mail_he_sent(self, world, title, source_id):
        person = self._one_way(world)
        world.interaction(person, title=title, source_id=source_id)
        decision = world.decision(person)
        assert decision.verdict.wrote_to_own_address
        assert "wrote_to" in decision.protected_by

    @pytest.mark.parametrize("title,source_id", [("→ Hello", f"abc:{OWN_ADDRESS}"), ("→ Hello", "plainid")])
    def test_a_sent_message_is_not_received_mail(self, world, title, source_id):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.interaction(person, title=title, source_id=source_id)
        decision = world.decision(person)
        assert decision.received == 0 and decision.sent == 1

    @pytest.mark.parametrize("arrow,received,thread", [("←", 1, 0), ("↔", 0, 1)])
    def test_an_arrow_outweighs_a_colon_in_the_source_id(self, world, arrow, received, thread):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.interaction(person, title=f"{arrow} Hello", source_id="abc:def")
        decision = world.decision(person)
        assert (decision.received, decision.thread, decision.sent) == (received, thread, 0)

    def test_a_sent_message_counts_for_the_address_it_went_to(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS, "events@acmewidgets.com"])
        world.mail(person, count=2, address=OWN_ADDRESS)
        world.interaction(person, title="→ Hello", source_id="abc:events@acmewidgets.com")
        decision = world.decision(person)
        assert decision.sent == 1
        assert not decision.verdict.wrote_to_own_address

    def test_mail_he_sent_recorded_only_as_a_source_entity(self, world):
        person = self._one_way(world)
        world.source_entity(person, source_id=f"abc:{OWN_ADDRESS}", email=OWN_ADDRESS)
        assert "wrote_to" in world.decision(person).protected_by

    def test_wrote_to_saves_a_bulk_row(self, world):
        person = self._one_way(world)
        world.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")
        decision = world.decision(person, {person.id: {"checked": 3, "bulk": 3}})
        assert decision.verdict.is_service and "wrote_to" in decision.protected_by
        assert not decision.will_hide

    def test_a_shared_thread_is_not_mail_he_sent(self, world):
        person = self._one_way(world)
        world.interaction(person, title="↔ Hello", source_id=f"abc:cc:{OWN_ADDRESS}")
        world.source_entity(person, source_id=f"abc:cc:{OWN_ADDRESS}", email=OWN_ADDRESS)
        decision = world.decision(person)
        assert decision.thread == 1 and decision.sent == 0
        assert not decision.verdict.is_service

    @pytest.mark.parametrize("source,attendees,protected", [
        ("calendar", 1, True), ("calendar", 5, True), ("calendar", 6, False), ("calendar", None, False),
        ("calendar", 0, False),
        ("meeting", None, True), ("vault", None, False),
    ])
    def test_a_real_meeting(self, world, source, attendees, protected):
        person = self._one_way(world)
        world.interaction(person, source=source, title="Event", attendees=attendees)
        decision = world.decision(person)
        assert ("real_meeting" in decision.protected_by) is protected
        assert decision.will_hide is not protected

    @pytest.mark.parametrize("status,email,protected", [
        ("confirmed", OWN_ADDRESS, True), ("manual", OWN_ADDRESS, True), ("rejected", OWN_ADDRESS, False),
        ("confirmed", None, True),
    ])
    def test_a_human_link_on_their_own_address(self, world, status, email, protected):
        person = self._one_way(world)
        world.source_entity(person, status=status, email=email)
        decision = world.decision(person)
        assert ("human_link" in decision.protected_by) is protected
        assert decision.will_hide is not protected


class TestDecisions:
    def test_address_comes_from_mail_when_the_row_has_none(self, world):
        person = world.person(name="Acme", emails=[])
        world.mail(person, address="learn@connect.acmewidgets.com")
        decision = world.decision(person)
        assert decision.addresses == {"learn@connect.acmewidgets.com": 6}
        assert decision.will_hide

    def test_a_row_with_no_email_is_not_judged(self, world):
        person = world.person(name="Acme", emails=[])
        world.interaction(person, source="vault", title="Note")
        assert person.id not in {d.person.id for d in world.decisions()}

    def test_a_row_with_an_address_and_no_mail_is_not_judged(self, world):
        person = world.person()
        assert person.id not in {d.person.id for d in world.decisions()}

    def test_a_row_with_mail_but_no_known_address_is_not_judged(self, world):
        person = world.person(name="Acme", emails=[])
        world.interaction(person)
        assert person.id not in {d.person.id for d in world.decisions()}

    def test_a_row_only_written_to_is_judged(self, world):
        person = world.person()
        world.interaction(person, title="→ Hello", source_id=f"abc:{SERVICE_ADDRESS}")
        assert world.decision(person).will_hide

    def test_a_hidden_row_is_not_judged(self, world):
        person = world.service()
        world.people.hide_person(person.id, "by hand")
        assert person.id not in {d.person.id for d in world.decisions()}

    def test_counts_and_stored_bulk_flags(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.mail(person, count=3, address=OWN_ADDRESS, metadata={"bulk": True})
        world.mail(person, count=1, address=OWN_ADDRESS, metadata={"bulk": False})
        world.mail(person, count=2, address=OWN_ADDRESS)
        decision = world.decision(person)
        assert decision.received == 6
        assert decision.verdict.reason == "bulk_mail"

    def test_too_little_stored_bulk_keeps_a_person(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.mail(person, count=1, address=OWN_ADDRESS, metadata={"bulk": True})
        world.mail(person, count=2, address=OWN_ADDRESS, metadata={"bulk": False})
        assert not world.decision(person).verdict.is_service

    def test_sampled_bulk_counts(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.mail(person, address=OWN_ADDRESS)
        assert not world.decision(person).will_hide
        assert world.decision(person, {person.id: {"checked": 3, "bulk": 3}}).will_hide
        assert not world.decision(person, {person.id: {"checked": 3, "bulk": 1}}).will_hide

    def test_malformed_metadata_does_not_cost_anyone_else_their_mail(self, world):
        person = world.person(name="Acme", emails=[])
        world.mail(person, address="learn@connect.acmewidgets.com")
        other = world.service(name="Northwind Bank", emails=["service@northwindtrading.com"])
        world.execute_sql("UPDATE source_entities SET metadata = '{broken' WHERE canonical_person_id = ?", other.id)
        assert world.decision(person).addresses == {"learn@connect.acmewidgets.com": 6}

    def test_a_missing_table_reads_as_empty(self, world):
        conn = sqlite3.connect(world.crm_db)
        assert ss._rows(conn, "SELECT * FROM no_such_table_here") == []
        conn.close()

    def test_any_other_failure_stops_the_run_rather_than_dropping_protection(self, world):
        conn = sqlite3.connect(world.crm_db)
        with pytest.raises(sqlite3.OperationalError):
            ss._rows(conn, "SELECT json_extract('{broken', '$.x')")
        conn.close()

    def test_thread_counts_reach_the_verdict(self, world):
        person = world.person(name="Pat Lee", emails=["desk@northwindtrading.com"])
        world.mail(person, count=50, address="desk@northwindtrading.com")
        assert world.decision(person).will_hide
        for i in range(6):
            world.interaction(person, title="↔ Subject", source_id=f"t{i}:cc:desk@northwindtrading.com")
        decision = world.decision(person)
        assert decision.thread == 6
        assert not decision.verdict.is_service

    def test_name_signals_see_the_domain_for_a_human_link(self, world):
        address = "northwindco@northwind.com"
        person = world.person(name="Northwindco", emails=[address])
        world.mail(person, count=2, address=address)
        world.source_entity(person, status="confirmed", email=address)
        assert "human_link" not in world.decision(person).protected_by


class TestHide:
    def test_hiding_clears_only_the_row_and_its_value(self, world):
        person = world.service(relationship_strength=42.0, dunbar_circle=5)
        world.hide(person)

        row = world.people.get_by_id(person.id)
        assert row.hidden and row.hidden_at is not None
        assert row.hidden_reason == "service sender: role_address"
        assert is_hidden_service(row)
        assert row.relationship_strength == 0.0
        assert row.dunbar_circle is None
        assert row.is_peripheral_contact
        assert len(world.interactions()) == 6
        conn = sqlite3.connect(world.crm_db)
        linked = conn.execute(
            "SELECT COUNT(*) FROM source_entities WHERE canonical_person_id = ?", (person.id,)
        ).fetchone()[0]
        conn.close()
        assert linked == 6

    def test_every_service_address_it_wrote_from_comes_with_it(self, world):
        person = world.person(name="Acme", emails=[])
        world.mail(person, address="learn@connect.acmewidgets.com")
        world.hide(person)
        assert world.people.is_blocked("learn@connect.acmewidgets.com")
        assert PersonEntityStore(str(world.crm_db)).is_blocked("learn@connect.acmewidgets.com")
        assert world.people.get_by_email("learn@connect.acmewidgets.com").id == person.id

    def test_an_address_someone_else_holds_stays_theirs(self, world):
        real = world.person(name="Pat Lee", emails=["shared@northwindtrading.com"], phone_numbers=["+15555550100"])
        person = world.person(name="Acme", emails=[])
        world.mail(person, count=9, address="learn@connect.acmewidgets.com")
        world.mail(person, count=1, address="shared@northwindtrading.com")
        world.hide(person)
        assert not world.people.is_blocked("shared@northwindtrading.com")
        assert world.people.get_by_email("shared@northwindtrading.com").id == real.id

    def test_a_persons_own_address_is_not_blocked_by_a_newsletter_on_their_row(self, world):
        gmail = "jane.sample@gmail.com"
        person = world.person(name="Jane Sample", emails=["newsletter@northwindtrading.com", gmail])
        world.mail(person, count=12, address="newsletter@northwindtrading.com")
        world.mail(person, count=8, address=gmail)
        decision = world.decision(person)
        assert decision.verdict.reason == "one_way_volume"
        assert gmail in decision.verdict.own_addresses
        world.hide(person)
        assert world.people.is_blocked("newsletter@northwindtrading.com")
        assert not world.people.is_blocked(gmail)

    def test_an_own_address_only_seen_on_mail_stays_free(self, world):
        person = world.person(name="Jane Sample", emails=["newsletter@northwindtrading.com"])
        world.mail(person, count=12, address="newsletter@northwindtrading.com")
        world.mail(person, count=8, address="jane.sample@gmail.com")
        world.hide(person)
        assert world.people.get_by_email("jane.sample@gmail.com") is None

    def test_a_hidden_service_takes_no_circle_slot(self, world):
        service = world.service(relationship_strength=99.0, is_peripheral_contact=False)
        people = [
            world.person(name=f"Sample Person{i}", emails=[], relationship_strength=90.0 - i, category="personal")
            for i in range(8)
        ]
        compute_all_dunbar_circles(world.people)
        assert world.people.get_by_id(service.id).dunbar_circle == 1
        assert world.people.get_by_id(people[4].id).dunbar_circle == 2

        world.hide(service)
        compute_all_dunbar_circles(world.people)

        assert world.people.get_by_id(service.id).dunbar_circle is None
        assert world.people.get_by_id(people[4].id).dunbar_circle == 1

    def test_a_blocked_address_cannot_become_a_new_person(self, world):
        person = world.person(name=SERVICE_NAME, emails=[])
        world.mail(person)
        world.hide(person)
        world.execute_sql("DELETE FROM person_emails")
        store = PersonEntityStore(str(world.crm_db))
        assert store.add(PersonEntity(canonical_name="Acme Again", emails=[SERVICE_ADDRESS])) is None


class TestAHiddenNamesake:
    def _hidden_namesake(self, world):
        person = world.person(name="Jane Sample", emails=["events@mail.acmewidgets.com"])
        world.mail(person, address="events@mail.acmewidgets.com")
        world.hide(person)
        return person

    def test_a_hidden_service_does_not_answer_to_its_name(self, world):
        hidden = self._hidden_namesake(world)
        assert world.people.get_by_name("Jane Sample") is None
        resolver = EntityResolver(world.people)
        assert resolver.resolve(name="Jane Sample") is None or resolver.resolve(name="Jane Sample").entity.id != hidden.id

    def test_it_does_not_absorb_a_different_address(self, world):
        hidden = self._hidden_namesake(world)
        result = EntityResolver(world.people).resolve(
            name="Jane Sample", email="jane.sample@gmail.com", create_if_missing=True,
        )
        assert result.entity.id != hidden.id
        assert not result.entity.hidden
        assert result.entity.has_email("jane.sample@gmail.com")

    def test_a_stale_name_entry_still_does_not_win(self, world):
        hidden = self._hidden_namesake(world)
        world.execute_sql("INSERT INTO person_names (name, person_id) VALUES ('jane sample', ?)", hidden.id)
        result = EntityResolver(world.people).match_name("Jane Sample")
        assert result.result is None or result.result.entity.id != hidden.id

    def test_hiding_does_not_take_the_name_from_a_visible_twin(self, world):
        twin = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        service = world.person(name="Jane Sample", emails=["events@mail.acmewidgets.com"])
        assert world.people.get_by_name("Jane Sample").id == service.id, "the later row holds the name"
        world.mail(service, address="events@mail.acmewidgets.com")
        world.hide(service)
        assert world.people.get_by_name("Jane Sample").id == twin.id

    def test_its_own_address_still_finds_it(self, world):
        hidden = self._hidden_namesake(world)
        result = EntityResolver(world.people).resolve(name="Jane Sample", email="events@mail.acmewidgets.com")
        assert result.entity.id == hidden.id

    def test_restore_gives_the_name_back(self, world):
        hidden = self._hidden_namesake(world)
        ss.restore_person(world.people, hidden.id)
        assert world.people.get_by_name("Jane Sample").id == hidden.id

    def test_other_hidden_rows_keep_their_full_names(self, world):
        person = world.person(name="Pat Lee", emails=["pat@northwindtrading.com"])
        world.people.hide_person(person.id, "fake marketing persona")
        assert world.people.get_by_name("Pat Lee").id == person.id
        assert EntityResolver(world.people).match_name("Pat Lee").result.entity.id == person.id


class TestRestore:
    def test_restore_undoes_the_hide_and_keeps_it(self, world):
        person = world.person(name="Acme", emails=[SERVICE_ADDRESS], relationship_strength=42.0)
        world.mail(person, address="learn@connect.acmewidgets.com")
        world.hide(person)

        restored = ss.restore_person(world.people, person.id)

        row = world.people.get_by_id(person.id)
        assert restored.id == person.id
        assert not row.hidden and row.hidden_at is None and row.hidden_reason == ""
        assert ss.CONFIRMED_PERSON_TAG in row.tags
        assert not world.people.is_blocked(SERVICE_ADDRESS)
        assert not world.people.is_blocked("learn@connect.acmewidgets.com")
        assert PersonEntityStore(str(world.crm_db)).is_blocked(SERVICE_ADDRESS) is False
        assert world.decision(person).protected_by == ["tags"]
        assert not world.decision(person).will_hide

    def test_restore_of_an_unknown_id(self, world):
        assert ss.restore_person(world.people, "no-such-person") is None

    def test_unhide_unblocks_a_phone_number(self, world):
        person = world.person(name="Pat Lee", emails=[], phone_numbers=["+15555550100"])
        world.people.hide_person(person.id, "x")
        assert world.people.is_blocked("+15555550100")
        world.people.unhide_person(person.id)
        assert not world.people.is_blocked("+15555550100")

    def test_inferred_relationships_come_back_from_the_kept_interactions(self, world, monkeypatch):
        """The whole round trip: hide, verify, restore, discover."""
        from api.services import relationship_discovery as discovery

        person = world.newsletter()
        conn = sqlite3.connect(world.interactions_db)
        conn.execute("UPDATE interactions SET title = '← Same subject ' || (rowid % 2)")
        conn.commit()
        conn.close()
        relationships = RelationshipStore(str(world.crm_db))
        monkeypatch.setattr("config.settings.settings.my_person_id", "owner", raising=False)
        monkeypatch.setattr(discovery, "get_relationship_store", lambda: relationships)
        monkeypatch.setattr(discovery, "get_interaction_db_path", lambda: str(world.interactions_db))

        def rows():
            conn = sqlite3.connect(world.crm_db)
            found = conn.execute(
                "SELECT shared_threads_count FROM relationships WHERE person_a_id = ? OR person_b_id = ?",
                (person.id, person.id),
            ).fetchall()
            conn.close()
            return found

        discovery.discover_from_email_threads()
        before = rows()
        assert before, "discovery should find the owner's threads with this row"

        world.hide(person)
        with patch("config.settings.settings.chroma_path", world.root / "chromadb"):
            consistency._check_relationship_hygiene({person.id}, {person.id, "a", "b", "c"}, dry_run=False)
        assert rows() == []
        assert len(world.interactions()) == T

        ss.restore_person(world.people, person.id)
        discovery.discover_from_email_threads()
        assert rows() == before


class TestAutoRestore:
    @pytest.fixture
    def nightly(self, world, monkeypatch):
        monkeypatch.setattr(PersonEntityStore, "MERGED_IDS_PATH", world.merged)
        with patch("api.services.interaction_store.get_interaction_db_path", return_value=str(world.interactions_db)):
            yield world

    def test_two_way_mail_on_an_own_address_brings_a_row_back(self, nightly):
        person = nightly.newsletter()
        assert ss.hide_service_senders(nightly.people)["hidden"] == 1
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")

        result = ss.hide_service_senders(nightly.people)

        assert result == {"hidden": 0, "held_back": 0, "restored": 1}
        row = nightly.people.get_by_id(person.id)
        assert not row.hidden
        assert not nightly.people.is_blocked(OWN_ADDRESS)
        assert ss.hide_service_senders(nightly.people)["hidden"] == 0

    def test_a_hold_back_still_restores_and_says_so(self, nightly, monkeypatch):
        person = nightly.newsletter()
        ss.hide_service_senders(nightly.people)
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")
        monkeypatch.setattr(ss, "NIGHTLY_MAX_HIDES", 0)
        nightly.service()
        assert ss.hide_service_senders(nightly.people) == {"hidden": 0, "held_back": 1, "restored": 1}

    @pytest.mark.parametrize("name,address", [("Robert Sample", "bob@northwind.com"), ("Pat Lee", "cq@northwind.com")])
    def test_a_reply_to_an_address_without_their_name(self, nightly, name, address):
        person = nightly.person(name=name, emails=[address])
        nightly.mail(person, count=20, address=address)
        assert ss.hide_service_senders(nightly.people)["hidden"] == 1
        assert nightly.people.is_blocked(address)

        nightly.interaction(person, title="→ Hello", source_id=f"abc:{address}")

        assert ss.hide_service_senders(nightly.people)["restored"] == 1
        assert not nightly.people.get_by_id(person.id).hidden
        assert not nightly.people.is_blocked(address)

    def test_a_reply_to_a_companys_support_mailbox_does_not(self, nightly):
        address = "support@blueapron.com"
        person = nightly.person(name="Blue Apron", emails=[address])
        nightly.mail(person, address=address)
        ss.hide_service_senders(nightly.people)
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{address}")
        assert ss.hide_service_senders(nightly.people)["restored"] == 0
        assert nightly.people.get_by_id(person.id).hidden

    def test_a_reply_to_a_role_mailbox_does_not(self, nightly):
        person = nightly.service()
        ss.hide_service_senders(nightly.people)
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{SERVICE_ADDRESS}")
        assert ss.hide_service_senders(nightly.people)["restored"] == 0
        assert nightly.people.get_by_id(person.id).hidden

    def test_a_sent_message_with_nothing_back_is_not_two_way(self, nightly):
        person = nightly.person(name="Jane Sample", emails=[OWN_ADDRESS])
        person.hidden = True
        person.hidden_reason = "service sender: one_way_volume"
        nightly.people.update(person)
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")
        assert ss.hide_service_senders(nightly.people)["restored"] == 0

    def test_a_row_hidden_for_another_reason_is_left_alone(self, nightly):
        person = nightly.newsletter()
        nightly.people.hide_person(person.id, "by hand")
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")
        assert ss.hide_service_senders(nightly.people)["restored"] == 0
        assert nightly.people.get_by_id(person.id).hidden

    def test_a_merged_away_row_is_left_alone(self, nightly):
        person = nightly.newsletter()
        ss.hide_service_senders(nightly.people)
        nightly.interaction(person, title="→ Hello", source_id=f"abc:{OWN_ADDRESS}")
        nightly.merged.write_text(json.dumps({person.id: "survivor"}))
        assert ss.hide_service_senders(nightly.people)["restored"] == 0


class TestConsistencyVerification:
    def test_hidden_services_keep_interactions_and_others_lose_them(self, world):
        service = world.service()
        world.hide(service)
        other = world.person(name="Spam Persona", emails=["spam@northwindtrading.com"])
        world.interaction(other)
        world.people.hide_person(other.id, "fake marketing persona")

        everyone = world.people.get_all(include_hidden=True, include_merged=True)
        ids = ({p.id for p in everyone}, {p.id for p in everyone if p.hidden}, world.people)
        # Hidden rows are a majority here, so the bulk-delete guard has to be cleared.
        ballast = {f"visible-{i}" for i in range(10)}
        quiet = {"count": 0, "fixed": 0}
        with patch.object(consistency, "_get_valid_person_ids", return_value=(ids[0] | ballast, ids[1], ids[2])), \
             patch.object(consistency, "_check_person_stats", return_value=quiet), \
             patch.object(consistency, "_check_last_seen", return_value=quiet), \
             patch.object(consistency, "_check_orphaned_interactions", return_value=quiet), \
             patch.object(consistency, "_check_stale_merged_ids", return_value=quiet), \
             patch.object(consistency, "_check_stale_merged_relationships", return_value=quiet), \
             patch.object(consistency, "_check_relationship_hygiene", return_value=quiet), \
             patch.object(consistency, "_check_orphaned_crm_records", return_value=quiet), \
             patch.object(consistency, "_check_missing_vault_files", return_value=quiet), \
             patch("api.services.interaction_store.get_interaction_db_path", return_value=str(world.interactions_db)):
            result = consistency.verify_consistency(dry_run=False, fix_threshold=1)

        assert result["hidden_interactions"]["fixed"] == 1
        conn = sqlite3.connect(world.interactions_db)
        remaining = dict(conn.execute("SELECT person_id, COUNT(*) FROM interactions GROUP BY person_id"))
        conn.close()
        assert remaining == {service.id: 6}


class TestNightlyPass:
    @pytest.fixture
    def nightly(self, world, monkeypatch):
        monkeypatch.setattr(PersonEntityStore, "MERGED_IDS_PATH", world.merged)
        with patch("api.services.interaction_store.get_interaction_db_path", return_value=str(world.interactions_db)):
            yield world

    def test_hides_new_services(self, nightly):
        person = nightly.service()
        real = nightly.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        nightly.mail(real, address="jane.sample@gmail.com")

        assert ss.hide_service_senders(nightly.people) == {"hidden": 1, "held_back": 0, "restored": 0}
        assert nightly.people.get_by_id(person.id).hidden
        assert not nightly.people.get_by_id(real.id).hidden

    def test_holds_back_past_the_limit(self, nightly, monkeypatch, caplog):
        monkeypatch.setattr(ss, "NIGHTLY_MAX_HIDES", 2)
        people = [
            nightly.service(name=f"Acme Bank{i}", emails=[f"service{i}@acmewidgets.com"]) for i in range(3)
        ]
        for i, person in enumerate(people):
            nightly.mail(person, address=f"service{i}@acmewidgets.com")

        with caplog.at_level("WARNING"):
            assert ss.hide_service_senders(nightly.people) == {"hidden": 0, "held_back": 3, "restored": 0}

        assert not any(nightly.people.get_by_id(p.id).hidden for p in people)
        assert "held back" in caplog.text
        assert people[0].canonical_name not in caplog.text

    def test_the_shipped_limit_stops_an_unreviewed_first_run(self, nightly):
        """A first run over an existing CRM is the repair's job, not the nightly's."""
        people = [
            nightly.person(name=f"Acme Bank{i}", emails=[f"service{i}@acmewidgets.com"])
            for i in range(ss.NIGHTLY_MAX_HIDES + 1)
        ]
        assert ss.NIGHTLY_MAX_HIDES <= 100
        for person in people:
            nightly.interaction(person)
        assert ss.hide_service_senders(nightly.people)["held_back"] == len(people)

    def test_exactly_the_limit_goes_ahead(self, nightly, monkeypatch):
        monkeypatch.setattr(ss, "NIGHTLY_MAX_HIDES", 1)
        nightly.service()
        assert ss.hide_service_senders(nightly.people)["hidden"] == 1

    def test_a_restored_row_stays_visible(self, nightly):
        person = nightly.service()
        ss.hide_service_senders(nightly.people)
        ss.restore_person(nightly.people, person.id)
        assert ss.hide_service_senders(nightly.people)["hidden"] == 0
        assert not nightly.people.get_by_id(person.id).hidden

    def test_the_strength_step_hides_before_it_scores(self, capsys):
        from scripts import sync_strengths

        order = []
        with patch.object(sync_strengths, "hide_service_senders", side_effect=lambda: order.append("hide") or {"hidden": 2, "held_back": 0, "restored": 1}), \
             patch.object(sync_strengths, "update_all_strengths", side_effect=lambda: order.append("score") or {"updated": 1, "failed": 0, "total": 1}), \
             patch("sys.argv", ["sync_strengths.py", "--execute"]):
            sync_strengths.main()

        assert order == ["hide", "score"]
        stats = json.loads(capsys.readouterr().out.split("SYNC_STATS:")[1].splitlines()[0])
        assert stats["service_senders_hidden"] == 2
        assert stats["service_senders_held_back"] == 0
        assert stats["service_senders_restored"] == 1

    def test_a_failed_pass_still_scores_and_reports_it(self, capsys):
        from scripts import sync_strengths

        with patch.object(sync_strengths, "hide_service_senders", side_effect=sqlite3.OperationalError("locked")), \
             patch.object(sync_strengths, "update_all_strengths", return_value={"updated": 1, "failed": 0, "total": 1}) as score, \
             patch("sys.argv", ["sync_strengths.py", "--execute"]):
            sync_strengths.main()

        score.assert_called_once_with()
        stats = json.loads(capsys.readouterr().out.split("SYNC_STATS:")[1].splitlines()[0])
        assert stats["errors"] == 1
        assert stats["service_senders_restored"] == 0

    def test_a_hold_back_is_recorded_and_reaches_the_summary(self, capsys):
        from scripts import sync_strengths
        from scripts.run_all_syncs import _parse_sync_output, send_sync_summary_telegram

        with patch.object(sync_strengths, "hide_service_senders", return_value={"hidden": 0, "held_back": 900, "restored": 0}), \
             patch.object(sync_strengths, "update_all_strengths", return_value={"updated": 1, "failed": 0, "total": 1}), \
             patch("api.services.sync_health.record_sync_error") as recorded, \
             patch("sys.argv", ["sync_strengths.py", "--execute"]):
            sync_strengths.main()

        recorded.assert_called_once()
        assert recorded.call_args.kwargs["error_type"] == "service_senders_held_back"
        output = capsys.readouterr().out
        assert _parse_sync_output(output)["service_senders_held_back"] == 900
        assert _parse_sync_output(output)["errors"] == 0

        result = {
            "failed": 0, "succeeded": 1, "sources_run": 1, "failed_sources": [], "duration_seconds": 1,
            "results": {"strengths": {"success": True, "service_senders_held_back": 900}},
        }
        with patch("api.services.telegram.send_message", return_value=True) as send:
            send_sync_summary_telegram(result, trigger="test")
        message = send.call_args[0][0]
        assert "Service filter held back" in message and "900" in message
        assert "✅" not in message

    def test_a_quiet_night_says_nothing_in_the_summary(self):
        from scripts.run_all_syncs import send_sync_summary_telegram

        result = {
            "failed": 0, "succeeded": 1, "sources_run": 1, "failed_sources": [], "duration_seconds": 1,
            "results": {"strengths": {"success": True, "service_senders_held_back": 0}},
        }
        with patch("api.services.telegram.send_message", return_value=True) as send:
            send_sync_summary_telegram(result, trigger="test")
        assert "Service filter held back" not in send.call_args[0][0]

    def test_the_strength_dry_run_does_neither(self):
        from scripts import sync_strengths

        with patch.object(sync_strengths, "hide_service_senders") as hide, \
             patch.object(sync_strengths, "update_all_strengths") as score, \
             patch("sys.argv", ["sync_strengths.py"]):
            sync_strengths.main()

        hide.assert_not_called()
        score.assert_not_called()


def _email(message_id, sender, name, to=None, cc=None, bulk=False):
    return EmailMessage(
        message_id=message_id, thread_id=f"t{message_id}", subject="Subject", sender=sender, sender_name=name,
        date=datetime(2026, 6, 15, tzinfo=timezone.utc), snippet="snippet", source_account="personal",
        to=to, cc=cc, bulk=bulk,
    )


class TestGmailSync:
    def _run(self, world, emails, my_person_id="", fetched=None):
        from scripts.sync_gmail_calendar_interactions import sync_gmail_interactions

        gmail = MagicMock()
        by_id = {email.message_id: email for email in emails}
        gmail.service.users().messages().list().execute.return_value = {"messages": [{"id": i} for i in by_id]}

        def batch(ids, **kw):
            if fetched is not None:
                fetched.extend(ids)
            return {i: by_id[i] for i in ids}

        gmail.get_messages_batch.side_effect = batch
        cache = GmailSkipCache(db_path=str(world.root / "skip.db"))
        module = "scripts.sync_gmail_calendar_interactions"
        with patch(f"{module}.GmailService", return_value=gmail), \
             patch(f"{module}.get_gmail_skip_cache", return_value=cache), \
             patch(f"{module}.get_interaction_db_path", return_value=str(world.interactions_db)), \
             patch(f"{module}.get_source_entity_store", return_value=world.sources), \
             patch(f"{module}.get_entity_resolver", return_value=EntityResolver(world.people)), \
             patch(f"{module}.settings") as settings:
            settings.my_person_id = my_person_id
            stats = sync_gmail_interactions(account_type=GoogleAccount.PERSONAL, dry_run=False, days_back=30)
        return stats, cache

    def _source_entities(self, world):
        conn = sqlite3.connect(world.crm_db)
        rows = conn.execute("SELECT observed_email, metadata FROM source_entities").fetchall()
        conn.close()
        return rows

    def test_an_unknown_service_creates_nothing(self, world):
        stats, cache = self._run(world, [_email("m1", "events@mail.acmewidgets.com", "Acme")])
        assert world.people.get_all() == []
        assert world.interactions() == []
        assert self._source_entities(world) == []
        assert stats["service_skipped"] == 1
        assert cache.get_skipped_ids("personal") == {"m1"}

    def test_a_new_sender_of_bulk_mail_creates_nothing(self, world):
        stats, _ = self._run(world, [_email("m1", OWN_ADDRESS, "Jane Sample", bulk=True)])
        assert world.people.get_all() == []
        assert stats["service_skipped"] == 1

    def test_a_new_person_is_created_as_usual(self, world):
        stats, cache = self._run(world, [_email("m1", OWN_ADDRESS, "Jane Sample")])
        assert [p.canonical_name for p in world.people.get_all()] == ["Jane Sample"]
        assert len(world.interactions()) == 1
        assert json.loads(self._source_entities(world)[0][1])["bulk"] is False
        assert stats["service_skipped"] == 0
        assert cache.get_skipped_ids("personal") == set()

    @pytest.mark.parametrize("address", ["founder@sample.com", "me@janesample.com", "j@janesample.com"])
    def test_a_person_on_their_own_domain_gets_a_row_from_one_message(self, world, address):
        self._run(world, [_email("m1", address, "Jane Sample")])
        assert world.people.get_by_email(address) is not None
        assert len(world.interactions()) == 1

    def test_a_phone_number_stops_the_drop(self, world):
        person = world.person(name="Jane Sample", emails=[], phone_numbers=["+15555550100"])
        stats, _ = self._run(world, [_email("m1", "events@mail.acmewidgets.com", "Jane Sample")])
        assert stats["service_skipped"] == 0
        assert world.interactions() == [(person.id, "← Subject")]

    def test_a_phone_number_also_stops_the_marketing_drop(self, world):
        person = world.person(name="Jane Sample", emails=[], phone_numbers=["+15555550100"])
        stats, _ = self._run(world, [_email("m1", "hello@janes-letter.com", "Jane Sample")])
        assert stats["marketing_skipped"] == 0
        assert world.interactions() == [(person.id, "← Subject")]

    def test_a_hidden_namesake_with_a_phone_does_not_stop_it(self, world):
        hidden = world.person(name="Jane Sample", emails=["pat@northwindtrading.com"], phone_numbers=["+15555550100"])
        world.people.hide_person(hidden.id, "by hand")
        stats, _ = self._run(world, [_email("m1", "events@mail.acmewidgets.com", "Jane Sample")])
        assert stats["service_skipped"] == 1

    def test_a_namesake_with_no_phone_does_not_stop_it(self, world):
        world.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        stats, _ = self._run(world, [_email("m1", "events@mail.acmewidgets.com", "Jane Sample")])
        assert stats["service_skipped"] == 1
        assert world.interactions() == []

    def test_a_one_word_namesake_with_a_phone_does_not_stop_it(self, world):
        world.person(name="Acme", emails=[], phone_numbers=["+15555550100"])
        stats, _ = self._run(world, [_email("m1", "events@mail.acmewidgets.com", "Acme")])
        assert stats["service_skipped"] == 1

    def test_bulk_mail_from_a_known_person_counts_and_is_recorded(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        stats, _ = self._run(world, [_email("m1", OWN_ADDRESS, "Jane Sample", bulk=True)])
        assert world.interactions() == [(person.id, "← Subject")]
        assert json.loads(self._source_entities(world)[0][1])["bulk"] is True
        assert stats["service_skipped"] == 0

    def test_a_service_cannot_land_on_someone_sharing_its_name(self, world):
        acme = world.person(name="Acme Sample", emails=["acme.sample@gmail.com"])
        self._run(world, [_email("m1", "events@e.acmewidgets.com", "Acme Sample")])
        assert world.interactions() == []
        assert world.people.get_by_id(acme.id).emails == ["acme.sample@gmail.com"]

    def test_a_blocked_address_with_no_row_produces_nothing(self, world):
        world.people.hide_person(world.person(name="Gone", emails=["gone@northwindtrading.com"]).id, "x")
        world.execute_sql("DELETE FROM person_emails WHERE email = 'gone@northwindtrading.com'")
        stats, _ = self._run(world, [_email(
            "m1", "gone@northwindtrading.com", "Gone Sample", to="Pat Lee <pat.lee@gmail.com>",
        )])
        assert world.interactions() == []
        assert world.people.get_by_email("pat.lee@gmail.com") is None
        assert stats["service_skipped"] == 1

    def test_a_service_on_the_thread_creates_nothing(self, world):
        person = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        self._run(world, [_email(
            "m1", "jane.sample@gmail.com", "Jane Sample",
            to="Acme <desk@e.acmewidgets.com>", cc="Pat Lee <pat.lee@gmail.com>",
        )])
        assert world.people.get_by_email("desk@e.acmewidgets.com") is None
        pat = world.people.get_by_email("pat.lee@gmail.com")
        assert {row[0] for row in world.interactions()} == {person.id, pat.id}

    def test_a_sender_named_only_by_their_address_is_created_as_usual(self, world):
        self._run(world, [_email("m1", "robin@northwindtrading.com", "robin@northwindtrading.com")])
        assert [p.canonical_name for p in world.people.get_all()] == ["Robin"]

    @pytest.mark.parametrize("address,name", [
        ("hello@janesample.com", "Jane Sample"), ("info@janesample.com", "Jane Sample"),
        ("team@janesample.com", "Jane Sample"), ("support@janesample.com", "Jane Sample"),
    ])
    def test_replies_to_a_role_mailbox_are_counted_truthfully(self, world, address, name):
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        self._run(
            world, [_email("m1", "owner.sample@gmail.com", "Owner Sample", to=f"{name} <{address}>")],
            my_person_id=me.id,
        )
        recipient = world.people.get_by_email(address)
        assert recipient is not None
        assert world.decision(recipient).sent == 1

    def test_a_reply_to_a_companys_role_mailbox_is_counted_and_does_not_protect(self, world):
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        self._run(
            world, [_email("m1", "owner.sample@gmail.com", "Owner Sample", to="Acme Help <support@acmewidgets.com>")],
            my_person_id=me.id,
        )
        decision = world.decision(world.people.get_by_email("support@acmewidgets.com"))
        assert decision.sent == 1
        assert decision.will_hide

    def test_her_answer_from_a_role_address_counts_once_the_address_is_known(self, world):
        person = world.person(name="Jane Sample", emails=["hello@janesample.com"])
        stats, _ = self._run(world, [_email("m1", "hello@janesample.com", "Jane Sample")])
        assert stats["marketing_skipped"] == 0
        assert world.interactions() == [(person.id, "← Subject")]

    def test_mail_from_a_stranger_at_a_marketing_prefix_is_still_dropped(self, world):
        stats, _ = self._run(world, [_email("m1", "deals@mailchimp.com", "Jane Sample")])
        assert stats["marketing_skipped"] == 1
        assert world.interactions() == []

    def test_a_hidden_services_mail_is_kept_and_not_fetched_again(self, world):
        person = world.service()
        world.hide(person)
        before = len(world.interactions())
        fetched = []
        emails = [_email("m1", SERVICE_ADDRESS, SERVICE_NAME)]
        stats, cache = self._run(world, emails, fetched=fetched)
        assert len(world.interactions()) == before + 1
        assert stats["service_skipped"] == 0
        assert world.people.get_by_email(SERVICE_ADDRESS).hidden

        fetched.clear()
        self._run(world, emails, fetched=fetched)
        assert fetched == []

    def test_a_hidden_service_on_the_thread_is_kept_too(self, world):
        person = world.service()
        world.hide(person)
        sender = world.person(name="Pat Lee", emails=["pat.lee@gmail.com"])
        before = len(world.interactions())
        self._run(world, [_email("m1", "pat.lee@gmail.com", "Pat Lee", to=f"Acme <{SERVICE_ADDRESS}>")])
        assert sum(pid == person.id for pid, _ in world.interactions()) == 7
        assert sum(pid == sender.id for pid, _ in world.interactions()) == 1
        assert len(world.interactions()) == before + 2

    def test_someone_hidden_by_hand_still_gets_nothing(self, world):
        person = world.person(name="Pat Lee", emails=["pat.lee@gmail.com"])
        world.people.hide_person(person.id, "by hand")
        self._run(world, [_email("m1", "pat.lee@gmail.com", "Pat Lee")])
        assert world.interactions() == []

    def test_a_hidden_service_keeps_its_calendar_record(self, world):
        from api.services.calendar import CalendarEvent
        from scripts.sync_gmail_calendar_interactions import sync_calendar_interactions

        person = world.service()
        world.hide(person)
        before = len(world.interactions())
        start = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        event = CalendarEvent(
            event_id="ev1", title="Synthetic sync", start_time=start, end_time=start,
            source_account="personal", attendees=[SERVICE_ADDRESS],
        )
        calendar = MagicMock()
        calendar.get_events_in_range.return_value = [event]
        module = "scripts.sync_gmail_calendar_interactions"
        with patch(f"{module}.CalendarService", return_value=calendar), \
             patch(f"{module}.get_interaction_db_path", return_value=str(world.interactions_db)), \
             patch(f"{module}.get_source_entity_store", return_value=world.sources), \
             patch(f"{module}.get_entity_resolver", return_value=EntityResolver(world.people)):
            sync_calendar_interactions(account_type=GoogleAccount.PERSONAL, dry_run=False, days_back=30)
        assert len(world.interactions()) == before + 1

    def test_mail_the_owner_sends_a_hidden_service_is_kept_too(self, world):
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        person = world.service()
        world.hide(person)
        before = len(world.interactions())
        self._run(
            world, [_email("m1", "owner.sample@gmail.com", "Owner Sample", to=f"Acme <{SERVICE_ADDRESS}>")],
            my_person_id=me.id,
        )
        assert len(world.interactions()) == before + 1

    def test_a_namesakes_mail_stays_with_the_namesake(self, world):
        person = world.person(name="Jane Sample", emails=["events@mail.acmewidgets.com"])
        world.mail(person, address="events@mail.acmewidgets.com")
        world.hide(person)
        count = len(world.interactions())
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        self._run(world, [
            _email("m1", "jane.sample@gmail.com", "Jane Sample"),
            _email("m2", "owner.sample@gmail.com", "Owner Sample", to="Jane Sample <jane.sample@gmail.com>"),
        ], my_person_id=me.id)
        real = world.people.get_by_email("jane.sample@gmail.com")
        assert real is not None and real.id != person.id and not real.hidden
        assert sum(pid == real.id for pid, _ in world.interactions()) == 2
        assert len(world.interactions()) == count + 2

    def test_mail_skipped_before_someone_had_a_row_comes_back(self, world):
        first = _email("m1", OWN_ADDRESS, "Jane Sample", bulk=True)
        _, cache = self._run(world, [first])
        assert cache.get_skipped_ids("personal") == {"m1"}

        second = _email("m2", OWN_ADDRESS, "Jane Sample")
        _, cache = self._run(world, [first, second])
        assert cache.get_skipped_ids("personal") == set(), "a new person releases their skipped mail"
        person = world.people.get_by_email(OWN_ADDRESS)
        assert len(world.interactions()) == 1

        self._run(world, [first, second])
        assert sum(pid == person.id for pid, _ in world.interactions()) == 2

    def test_writing_to_someone_releases_their_skipped_mail_too(self, world):
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        first = _email("m1", OWN_ADDRESS, "Jane Sample", bulk=True)
        _, cache = self._run(world, [first], my_person_id=me.id)
        assert cache.get_skipped_ids("personal") == {"m1"}
        sent = _email("m2", "owner.sample@gmail.com", "Owner Sample", to=f"Jane Sample <{OWN_ADDRESS}>")
        _, cache = self._run(world, [first, sent], my_person_id=me.id)
        assert cache.get_skipped_ids("personal") == set()

    def test_a_new_person_on_the_thread_releases_their_skipped_mail_too(self, world):
        first = _email("m1", "pat.lee@gmail.com", "Pat Lee", bulk=True)
        _, cache = self._run(world, [first])
        assert cache.get_skipped_ids("personal") == {"m1"}
        world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        thread = _email("m2", OWN_ADDRESS, "Jane Sample", to="Pat Lee <pat.lee@gmail.com>")
        _, cache = self._run(world, [first, thread])
        assert cache.get_skipped_ids("personal") == set()

    def test_releasing_mail_only_asks_gmail_for_that_address(self, world):
        from scripts.sync_gmail_calendar_interactions import _release_skipped_mail

        gmail = MagicMock()
        gmail.service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
        cache = MagicMock()
        cache.forget.return_value = 1
        _release_skipped_mail(gmail, cache, "personal", {OWN_ADDRESS}, datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert gmail.service.users().messages().list.call_args.kwargs["q"] == f"from:{OWN_ADDRESS} after:2026/06/01"
        cache.forget.assert_called_once_with("personal", ["m1"])

    def test_someone_he_writes_to_is_recorded_however_they_look(self, world):
        me = world.person(name="Owner Sample", emails=["owner.sample@gmail.com"])
        self._run(
            world,
            [_email("m1", "owner.sample@gmail.com", "Owner Sample", to="Acme Help <desk@e.acmewidgets.com>")],
            my_person_id=me.id,
        )
        recipient = world.people.get_by_email("desk@e.acmewidgets.com")
        assert recipient is not None
        assert world.decision(recipient).sent == 1


class TestSkipCacheForget:
    def test_forget_clears_only_what_it_names(self, world):
        cache = GmailSkipCache(db_path=str(world.root / "skip.db"))
        cache.record_skipped("personal", ["m1", "m2"])
        cache.record_skipped("work", ["m1"])
        assert cache.forget("personal", ["m1", "other"]) == 1
        assert cache.get_skipped_ids("personal") == {"m2"}
        assert cache.get_skipped_ids("work") == {"m1"}
        assert cache.forget("personal", []) == 0


class TestIsUnknownServiceSender:
    def test_empty_address(self, world):
        assert not ss.is_unknown_service_sender(world.people, "", "Acme")

    def test_a_held_address_is_never_in_question(self, world):
        world.person(name="Acme", emails=["noreply@acmewidgets.com"])
        assert not ss.is_unknown_service_sender(world.people, "noreply@acmewidgets.com", "Acme", bulk=True)

    def test_a_blocked_address(self, world):
        world.people.hide_person(world.person(name="Pat Lee", emails=["pat@northwindtrading.com"]).id, "x")
        world.execute_sql("DELETE FROM person_emails")
        assert ss.is_unknown_service_sender(world.people, "PAT@northwindtrading.com", "Pat Lee")

    def test_case_and_missing_name(self, world):
        assert ss.is_unknown_service_sender(world.people, "NoReply@AcmeWidgets.com")
        assert not ss.is_unknown_service_sender(world.people, "jane.sample@gmail.com")

    def test_is_known_sender(self, world):
        world.person(name="Jane Sample", emails=["jane@janes-letter.com"])
        hidden_other = world.person(name="Pat Lee", emails=["pat@northwindtrading.com"])
        world.people.hide_person(hidden_other.id, "by hand")
        hidden_service = world.service()
        world.hide(hidden_service)
        assert ss.is_known_sender(world.people, "jane@janes-letter.com")
        assert ss.is_known_sender(world.people, SERVICE_ADDRESS)
        assert not ss.is_known_sender(world.people, "pat@northwindtrading.com")
        assert not ss.is_known_sender(world.people, "stranger@northwindtrading.com", "Sam Sample")


class TestRepairScript:
    def _hash(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_dry_run_writes_only_the_review(self, world, capsys):
        person = world.service()
        before = self._hash(world.crm_db), self._hash(world.interactions_db)
        store = world.run()
        assert (self._hash(world.crm_db), self._hash(world.interactions_db)) == before
        assert not store.get_by_id(person.id).hidden
        assert not list(world.root.glob("*.bak-*"))
        assert world.review.exists()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert person.canonical_name not in out
        assert SERVICE_ADDRESS not in out
        assert '"would_hide": 1' in out

    def test_review_is_private(self, world):
        world.service()
        world.run()
        assert stat.S_IMODE(os.stat(world.review).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(world.review.parent).st_mode) == 0o700

    def test_review_refuses_a_path_in_the_repository(self, world):
        world.service()
        inside = repair.REPO_ROOT / "tests" / "review.md"
        with pytest.raises(SystemExit):
            repair.main([
                "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
                "--merged-ids", str(world.merged), "--report", str(inside),
            ])
        assert not inside.exists()

    def test_samples_refuse_a_path_in_the_repository(self):
        with pytest.raises(SystemExit):
            repair._write_private(repair.REPO_ROOT / "tests" / "samples.json", "{}")

    def test_a_new_private_file_is_never_world_readable(self, world):
        target = world.root / "private" / "new.json"
        with patch.object(repair.os, "chmod"):
            repair._write_private(target, "{}")
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def _sections(self, world):
        return {section.split("\n")[0]: section for section in world.review.read_text().split("\n## ")}

    def _section(self, world, start):
        return next(v for k, v in self._sections(world).items() if k.startswith(start))

    def test_review_shows_both_error_directions(self, world):
        hidden = world.service(relationship_strength=42.0, dunbar_circle=5)
        protected = world.service(name="Northwind Bank", emails=["service@northwindtrading.com"], phone_numbers=["+15555550100"])
        short = world.person(name="Northwind Labs", emails=["hq@elsewhere-sample.com"])
        world.mail(short, count=1, address="hq@elsewhere-sample.com")
        world.run()
        reviewed = self._section(world, "Would hide, circles 0-5")
        assert hidden.canonical_name in reviewed and "role_address" in reviewed and hidden.id in reviewed
        saved = self._section(world, "Kept: service-shaped")
        assert protected.canonical_name in saved and "phone_number" in saved
        fell_short = self._section(world, "Kept: signals or one-way mail")
        assert short.canonical_name in fell_short and "organization_name" in fell_short

    def test_review_of_circles_leaves_out_lower_ones(self, world):
        high = world.service(relationship_strength=42.0, dunbar_circle=5)
        low = world.service(name="Northwind Bank", emails=["service@northwindtrading.com"], dunbar_circle=7)
        world.run()
        reviewed = self._section(world, "Would hide, circles 0-5")
        assert high.canonical_name in reviewed and low.canonical_name not in reviewed

    def test_review_samples_the_rest_at_random(self, world):
        for i in range(120):
            person = world.person(name=f"Acme Bank{i}", emails=[f"service{i}@acmewidgets.com"], dunbar_circle=7)
            world.mail(person, count=1, address=f"service{i}@acmewidgets.com")
        world.run()
        section = self._section(world, "Would hide, random")
        assert section.startswith("Would hide, random 100 of the other 120")
        assert section.count("\n| 7 |") == 100

    def test_review_shows_hidden_rows_with_the_most_two_way_mail(self, world):
        quiet = world.service()
        busy = world.person(name="Northwind Help", emails=["help@northwindtrading.com"])
        world.mail(busy, count=1, address="help@northwindtrading.com")
        for i in range(3):
            world.interaction(busy, title="↔ Subject", source_id=f"t{i}:cc:help@northwindtrading.com")
        world.interaction(busy, title="→ Subject", source_id="s1:help@northwindtrading.com")
        busier = world.person(name="Acme Help", emails=["help@acmewidgets.com"])
        for i in range(6):
            world.interaction(busier, title="→ Subject", source_id=f"s{i}:help@acmewidgets.com")
        world.run()
        section = self._section(world, "Hidden rows with the most two-way mail")
        assert section.index("Acme Help") < section.index("Northwind Help")
        assert quiet.canonical_name not in section

    def test_review_compares_with_the_last_version(self, world, tmp_path):
        new = world.service()
        same = world.service(name="Northwind Bank", emails=["service@northwindtrading.com"])
        kept_now = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        bystander = world.person(name="Bystander Sample", emails=["bystander.sample@gmail.com"])
        world.mail(bystander, address="bystander.sample@gmail.com")
        previous = tmp_path / "previous.json"
        previous.write_text(json.dumps([same.id, kept_now.id, "gone-now"]))
        world.mail(kept_now, address="jane.sample@gmail.com")
        world.run("--previous", str(previous))
        added = self._section(world, "Newly hidden compared with the last version (1)")
        assert new.canonical_name in added and same.canonical_name not in added
        removed = self._section(world, "Hidden last version, kept now (1)")
        assert kept_now.canonical_name in removed
        assert "Bystander" not in removed

    def test_review_says_why_a_row_was_kept(self, world, tmp_path):
        protected = world.service(phone_numbers=["+15555550100"])
        previous = tmp_path / "previous.json"
        previous.write_text(json.dumps([protected.id]))
        world.run("--previous", str(previous))
        assert "phone_number" in self._section(world, "Hidden last version, kept now")

    def test_no_comparison_without_a_previous_version(self, world):
        world.service()
        world.run()
        assert not any(k.startswith("Newly hidden") for k in self._sections(world))

    def test_summary_counts_by_circle_and_rank(self, world):
        for i, circle in enumerate([4, 5, 5, 7, None]):
            person = world.person(
                name=f"Acme Bank{i}", emails=[f"service{i}@acmewidgets.com"], dunbar_circle=circle,
                relationship_strength=50.0 - i,
            )
            world.mail(person, count=1, address=f"service{i}@acmewidgets.com")
        real = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"], relationship_strength=90.0, dunbar_circle=1)
        world.mail(real, address="jane.sample@gmail.com")
        summary = repair.summarize(world.decisions(), top_strength=48.0)
        assert summary["would_hide"] == 5
        assert summary["by_circle"] == {4: 1, 5: 2, 7: 1, None: 1}
        assert summary["in_top_500_by_strength"] == 3
        assert summary["people_with_email"] == 6
        assert summary["by_rule"] == {"role_address": 5}

    def test_summary_counts_what_each_protection_saves(self, world):
        world.service(phone_numbers=["+15555550100"])
        world.service(name="Northwind Bank", emails=["service@northwindtrading.com"], phone_numbers=["+15555550101"], company="Sample Co")
        real = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"], phone_numbers=["+15555550102"])
        world.mail(real, address="jane.sample@gmail.com")
        summary = repair.summarize(world.decisions(), top_strength=0.0)
        assert summary["service_shaped_but_protected"] == 2
        assert summary["protected_by"] == {"phone_number": 2, "company": 1}
        assert summary["protected_by_only_this"] == {"phone_number": 1}

    def test_top_rank_counts_everyone_visible_with_or_without_email(self, world, monkeypatch):
        monkeypatch.setattr(repair, "TOP_RANK", 2)
        world.person(name="Sample One", emails=[], phone_numbers=["+15555550100"], relationship_strength=90.0)
        world.person(name="Sample Two", emails=["sample.two@gmail.com"], relationship_strength=70.0)
        world.person(name="Sample Three", emails=[], relationship_strength=50.0)
        world.person(name="Sample Five", emails=[], relationship_strength=30.0)
        hidden = world.person(name="Sample Four", emails=[], relationship_strength=99.0)
        world.people.hide_person(hidden.id, "x")
        assert repair.top_rank_strength(world.crm_db) == 70.0

    def test_top_rank_on_a_small_crm_takes_everyone(self, world):
        world.person(name="Sample One", emails=[], relationship_strength=90.0)
        assert repair.top_rank_strength(world.crm_db) == 0.0

    def test_a_kept_row_that_never_looked_like_a_service_is_not_near_the_line_for_protection(self, world):
        real = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"], phone_numbers=["+15555550100"])
        world.mail(real, address="jane.sample@gmail.com")
        protected, _ = repair.near_line(world.decisions())
        assert protected == []

    def test_thinnest_protection_comes_first(self, world):
        thick = world.service(name="Northwind Bank", emails=["service@northwindtrading.com"], phone_numbers=["+15555550100"], tags=["x"])
        thin = world.service(phone_numbers=["+15555550101"])
        protected, _ = repair.near_line(world.decisions())
        assert [d.person.id for d in protected] == [thin.id, thick.id]

    def test_kept_rows_near_the_line(self, world):
        def kept(name, address, count=2):
            person = world.person(name=name, emails=[address])
            world.mail(person, count=count, address=address)
            return person

        signal = kept("Northwind Labs", "hq@elsewhere-sample.com", count=1)
        near = kept("Jane Sample", OWN_ADDRESS, count=T - 1)
        far = kept("Sam Sample", "sam@janes-letter.com", count=repair.NEAR_ONE_WAY_RECEIVED - 1)
        plain = kept("Pat Lee", "desk@northwindtrading.com", count=2)
        talkative = kept("Dana Sample", "dana@janes-letter.com", count=T - 1)
        world.interaction(talkative, title="↔ Subject", source_id="t:cc:dana@janes-letter.com")

        _, short = repair.near_line(world.decisions())
        ids = [d.person.id for d in short]
        assert near.id in ids and signal.id in ids
        assert far.id not in ids and plain.id not in ids and talkative.id not in ids
        assert ids[0] == near.id, "most mail first"

    def test_near_line_sections_are_capped_but_counted(self, world, monkeypatch):
        monkeypatch.setattr(repair, "NEAR_LINE_SIZE", 1)
        for i in range(2):
            world.service(name=f"Acme Bank{i}", emails=[f"service{i}@acmewidgets.com"], phone_numbers=[f"+1555555010{i}"])
            person = world.person(name=f"Northwind Labs {'Alpha' if i else 'Beta'}", emails=[f"hq{chr(97 + i)}@elsewhere-sample.com"])
            world.mail(person, count=1, address=f"hq{chr(97 + i)}@elsewhere-sample.com")
        text = repair.build_review(world.decisions(), {}, 0.0)
        saved = next(s for s in text.split("\n## ") if s.startswith("Kept: service-shaped"))
        short = next(s for s in text.split("\n## ") if s.startswith("Kept: signals or one-way mail"))
        assert saved.startswith("Kept: service-shaped, only protection saved them (2, thinnest first, showing 1)")
        assert short.startswith("Kept: signals or one-way mail short of a rule (2, most mail first, showing 1)")
        assert saved.count("service0@") + saved.count("service1@") == 1
        assert short.count("hqa@") + short.count("hqb@") == 1

    def test_two_way_section_is_capped(self, world, monkeypatch):
        monkeypatch.setattr(repair, "TWO_WAY_SIZE", 1)
        for i in range(2):
            person = world.person(name=f"Acme Help{chr(97 + i)}", emails=[f"help{i}@acmewidgets.com"])
            world.interaction(person, title="→ Subject", source_id=f"s{i}:help{i}@acmewidgets.com")
        text = repair.build_review(world.decisions(), {}, 0.0)
        section = next(s for s in text.split("\n## ") if s.startswith("Hidden rows with the most"))
        assert section.count("help0@") + section.count("help1@") == 1

    def test_merged_ids_path_is_honored(self, world, tmp_path):
        person = world.service()
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps({"merged-away": person.id}))
        repair.main([
            "--crm-db", str(world.crm_db), "--interactions-db", str(world.interactions_db),
            "--merged-ids", str(elsewhere), "--report", str(world.review),
        ])
        assert "would_hide\": 0" in world.review.read_text()

    def test_execute_backs_up_hides_and_is_idempotent(self, world, capsys):
        person = world.service(relationship_strength=42.0, dunbar_circle=5)
        real = world.person(name="Jane Sample", emails=["jane.sample@gmail.com"])
        world.mail(real, address="jane.sample@gmail.com")
        interactions_before = self._hash(world.interactions_db)

        store = world.run("--execute")

        backups = list(world.root.glob("crm.db.bak-service-senders-*"))
        assert len(backups) == 1
        assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
        conn = sqlite3.connect(backups[0])
        assert conn.execute("SELECT hidden FROM person_entities WHERE id = ?", (person.id,)).fetchone() == (0,)
        conn.close()
        assert store.get_by_id(person.id).hidden
        assert is_hidden_service(store.get_by_id(person.id))
        assert store.is_blocked(SERVICE_ADDRESS)
        assert not store.get_by_id(real.id).hidden
        assert self._hash(world.interactions_db) == interactions_before
        assert "Hidden: 1" in capsys.readouterr().out

        rows_before = [(p.id, p.hidden) for p in store.get_all(include_hidden=True)]
        store = world.run("--execute")
        assert "Hidden: 0" in capsys.readouterr().out
        assert [(p.id, p.hidden) for p in store.get_all(include_hidden=True)] == rows_before

    def test_execute_refuses_nothing_protected(self, world):
        person = world.service(phone_numbers=["+15555550100"])
        store = world.run("--execute")
        assert not store.get_by_id(person.id).hidden

    def test_restore_flag_says_what_came_back_and_what_did_not(self, world, capsys):
        person = world.service()
        world.run("--execute")
        capsys.readouterr()
        with patch.object(repair, "backup_crm", wraps=repair.backup_crm) as backup:
            store = world.run("--restore", person.id, "no-such-person")
        backup.assert_called_once_with(world.crm_db)
        row = store.get_by_id(person.id)
        assert not row.hidden and ss.CONFIRMED_PERSON_TAG in row.tags
        assert not store.is_blocked(SERVICE_ADDRESS)
        out = capsys.readouterr().out
        assert "Restored 1 of 2" in out
        assert "6 interactions" in out
        assert "inferred relationships" in out
        assert "Not recovered" in out
        assert person.canonical_name not in out

    def test_rerun_after_restore_leaves_it_visible(self, world):
        person = world.service()
        world.run("--execute")
        world.run("--restore", person.id)
        store = world.run("--execute")
        assert not store.get_by_id(person.id).hidden


class TestBulkSampling:
    def _person(self, world, name, address, timestamps=()):
        person = world.person(name=name, emails=[address])
        for i, when in enumerate(timestamps):
            world.interaction(person, source_id=f"{name}-{i}", when=when)
        return person

    def test_only_undecided_unprotected_rows_with_received_mail(self, world):
        undecided = self._person(world, "Jane Sample", OWN_ADDRESS, [NOW])
        decided = self._person(world, "Acme", "noreply@acmewidgets.com", [NOW])
        protected = self._person(world, "Pat Lee", "pat@northwindtrading.com", [NOW])
        world.execute_sql("UPDATE person_entities SET phone_numbers = '[\"+15555550100\"]' WHERE id = ?", protected.id)
        thread_only = world.person(name="Sam Lee", emails=["sam@northwindtrading.com"])
        world.interaction(thread_only, title="↔ Subject", source_id="a:cc:sam@northwindtrading.com")

        asked = []

        def fetch(account, ids):
            asked.extend(ids)
            return {i: True for i in ids}

        samples = repair.sample_bulk_headers(world.decisions(), world.interactions_db, {}, fetch)
        assert set(samples) == {undecided.id}
        assert asked == ["Jane Sample-0"]
        assert decided.id not in samples

    def test_latest_messages_per_account(self, world):
        person = self._person(world, "Jane Sample", OWN_ADDRESS, [
            "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00",
            "2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00",
        ])
        world.interaction(person, source_id="work-new", account="work", when="2026-05-01T00:00:00+00:00")
        asked = {}

        def fetch(account, ids):
            asked[account] = sorted(ids)
            return {i: True for i in ids}

        samples = repair.sample_bulk_headers(world.decisions(), world.interactions_db, {}, fetch)
        assert asked == {"personal": ["Jane Sample-2", "Jane Sample-3"], "work": ["work-new"]}
        assert samples[person.id] == {"checked": 3, "bulk": 3}

    def test_sampling_reads_received_mail_only(self, world):
        person = world.person(name="Jane Sample", emails=[OWN_ADDRESS])
        world.interaction(person, source_id="in-1", when="2026-01-01T00:00:00+00:00")
        world.interaction(person, title="↔ Subject", source_id="thread-1", when="2026-02-01T00:00:00+00:00")
        asked = []

        def fetch(account, ids):
            asked.extend(ids)
            return {}

        repair.sample_bulk_headers(world.decisions(), world.interactions_db, {}, fetch)
        assert asked == ["in-1"]

    def test_a_failed_account_leaves_its_rows_unsampled(self, world):
        person = self._person(world, "Jane Sample", OWN_ADDRESS, [NOW])
        other = self._person(world, "Pat Lee", "pat@northwindtrading.com", [NOW])
        conn = sqlite3.connect(world.interactions_db)
        conn.execute("UPDATE interactions SET source_account = 'work' WHERE person_id = ?", (other.id,))
        conn.commit()
        conn.close()

        def fetch(account, ids):
            if account == "work":
                raise RuntimeError("no token")
            return {i: False for i in ids}

        samples = repair.sample_bulk_headers(world.decisions(), world.interactions_db, {}, fetch)
        assert samples == {person.id: {"checked": 1, "bulk": 0}}

    def test_existing_samples_are_reused_not_fetched_again(self, world):
        person = self._person(world, "Jane Sample", OWN_ADDRESS, [NOW])
        fetch = MagicMock(return_value={})
        existing = {person.id: {"checked": 3, "bulk": 0}}
        samples = repair.sample_bulk_headers(world.decisions(existing), world.interactions_db, existing, fetch)
        fetch.assert_not_called()
        assert samples == existing

    def test_a_message_gmail_no_longer_has_is_not_counted(self, world):
        person = self._person(world, "Jane Sample", OWN_ADDRESS, [
            "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00",
        ])
        samples = repair.sample_bulk_headers(
            world.decisions(), world.interactions_db, {}, lambda account, ids: {"Jane Sample-0": True},
        )
        assert samples == {person.id: {"checked": 1, "bulk": 1}}

    def test_the_flag_samples_saves_and_a_later_run_reuses(self, world, capsys):
        person = self._person(world, "Jane Sample", OWN_ADDRESS, [NOW])
        samples_path = world.root / "private" / "samples.json"
        client = MagicMock()
        client.get_messages_batch.return_value = {"Jane Sample-0": MagicMock(bulk=True)}

        with patch.object(repair, "GmailService", return_value=client) as service:
            world.run("--sample-gmail", "--bulk-samples", str(samples_path))

        service.assert_called_once_with(account_type=GoogleAccount.PERSONAL)
        client.get_messages_batch.assert_called_once_with(["Jane Sample-0"], include_body=False)
        assert json.loads(samples_path.read_text()) == {person.id: {"checked": 1, "bulk": 1}}
        assert stat.S_IMODE(os.stat(samples_path).st_mode) == 0o600
        assert '"would_hide": 1' in capsys.readouterr().out

        with patch.object(repair, "GmailService") as service:
            store = world.run("--bulk-samples", str(samples_path), "--execute")
        service.assert_not_called()
        assert store.get_by_id(person.id).hidden

    def test_missing_samples_file_means_no_samples(self, world):
        assert repair.load_samples(world.root / "absent.json") == {}
        assert repair.load_samples(None) == {}
