"""Ownership and publication boundaries; all profiles/queues are temporary."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import tempfile
import unittest

from long_task_callback import callback_transport, cli, pi_callback as bridge


def build_bound(tmp_path):
    os.environ['PI_CODING_AGENT_DIR'] = str(tmp_path / 'pi')
    os.environ['CODEX_HOME'] = str(tmp_path / 'codex')
    os.environ[cli.TARGET_LOCK_DIR_ENV] = str(tmp_path / 'target-locks')
    os.environ.pop(bridge.CHANNEL_ROOT_ENV, None)
    session = tmp_path / 'session 中文.jsonl'
    session.write_text(json.dumps({'type': 'session', 'version': 3, 'id': 'session-fixture',
                                  'cwd': str(tmp_path), 'timestamp': '2026-10-04T00:00:00.000Z'}) + '\n')
    args = argparse.Namespace(agent='pi', session=str(session), last=False, cwd=str(tmp_path),
                              task='fixture', callback_mode='cli')
    request = cli.make_request(args, 'Wake the same Pi')
    request['id'] = 'fixture'
    queue = tmp_path / 'queue'; cli.ensure_daemon_dirs(queue)
    directory = bridge.channel_dir(str(session), Path(request['pi_channel_root']))
    bridge.ensure_private_directory(Path(request['pi_channel_root']))
    bridge.ensure_private_directory(directory)
    owner = {'version': 1, 'session_file': str(session), 'session_id': 'session-fixture',
             'profile_dir': request['pi_profile_dir'], 'channel_dir': str(directory),
             'owner_nonce': 'fixture-owner', 'owner_pid': os.getpid(), 'managed': False, 'state': 'active',
             'delivery_modes': ['follow-up', 'steer']}
    cli.write_request(directory / 'owner.json', owner)
    payload = {'request': request, 'queue_dir': str(queue), 'prompt': request['prompt'],
               'ack_path': str(cli.ack_path(queue, 'fixture')),
               'canceled_path': str(cli.request_path(queue, 'canceled', 'fixture')),
               'command': cli.resume_command(request), 'cwd': str(tmp_path), 'timeout': 1}
    return request, owner, directory, queue, payload


def dead_managed(owner, directory):
    owner.update(owner_pid=99999999, managed=True, state='closed',
                 owner_identity=cli.daemon_process_identity(os.getpid()))
    cli.write_request(directory / 'owner.json', owner)


class PiLiveCallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ltc-pi-live-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        patch = mock.patch.dict(os.environ)
        patch.start(); self.addCleanup(patch.stop)
        self.bound = build_bound(self.directory)

    def test_automatic_steer_and_frozen_managed_callback_roundtrip(self):
        request, _, _, queue, _ = self.bound
        args = argparse.Namespace(agent='pi', session=request['target']['value'], last=False,
                                  cwd=request['cwd'], task='fixture', callback_mode='cli')
        steer = cli.make_request(args, 'Steer with completed results')
        assert steer['version'] == 4 and steer['pi_delivery'] == 'steer'
        steer['id'] = 'fixture'
        path = queue/'pending/fixture.json'
        cli.write_request(path, steer)
        assert cli.load_request(path)['pi_delivery'] == 'steer'
        task = dict(steer, version=5, queue_dir=str(queue), execution_backend='screen',
                    screen_session='ltc-fixture', state='completed', command='/bin/true',
                    wrapped_command=['/bin/true'], log_path=str(self.directory/'task.log'))
        task_path = self.directory/'fixture/task.json'
        cli.write_request(task_path, task)
        loaded = cli.load_managed_task(task_path)
        with mock.patch.dict(os.environ, {bridge.PROFILE_ENV: str(self.directory/'other-profile')}):
            restored = cli.managed_callback_request(loaded, 'Saved result')
        assert restored['version'] == 4 and restored['pi_delivery'] == 'steer'
        task_path.write_text(json.dumps(dict(task, pi_delivery='invalid')))
        with self.assertRaisesRegex(ValueError, 'Pi delivery'):
            cli.load_managed_task(task_path)

    def test_legacy_follow_up_and_invalid_delivery(self):
        request, _, _, queue, _ = self.bound
        legacy = dict(request)
        legacy.pop('pi_delivery')
        legacy['version'] = 3
        path = queue/'pending/fixture.json'
        cli.write_request(path, legacy)
        assert cli.load_request(path)['version'] == 3
        assert bridge.inspect_route(legacy)['status'] == 'owner_registered'
        cli.write_request(path, dict(request, pi_delivery='invalid'))
        with self.assertRaisesRegex(ValueError, 'Pi delivery'):
            cli.load_request(path)
        args = argparse.Namespace(agent='codex', callback_mode='cli')
        assert 'pi_delivery' not in callback_transport.selection(args)

    def test_retry_keeps_steer_and_original_profile(self):
        request, _, _, queue, _ = self.bound
        cli.write_request(queue/'failed/fixture.json', request)
        args = argparse.Namespace(queue_dir=str(queue), id='fixture', callback_mode='cli')
        with mock.patch.dict(os.environ, {bridge.PROFILE_ENV: str(self.directory/'other-profile')}):
            assert cli.retry_callback(args) == 0
        restored = cli.load_request(queue/'pending/fixture.json')
        assert restored['version'] == 4 and restored['pi_delivery'] == 'steer'
        for key in bridge.ROUTE_FIELDS:
            assert restored[key] == request[key]

    def test_steer_requires_live_extension_capability_and_preserves_intent(self):
        request, owner, directory, queue, payload = self.bound
        request.update(version=4, pi_delivery='steer')
        owner.pop('delivery_modes')
        cli.write_request(directory/'owner.json', owner)
        assert bridge.inspect_route(request)['status'] == 'blocked'
        with self.assertRaisesRegex(cli.CallbackTransportBlocked, 'does not support steer'):
            bridge.prepare_delivery(payload)
        assert not (directory/'inbox').exists()
        assert not cli.retained_target_lease_is_held(request)
        owner['delivery_modes'] = ['follow-up', 'steer']
        cli.write_request(directory/'owner.json', owner)
        assert bridge.inspect_route(request)['status'] == 'owner_registered'
        assert bridge.prepare_delivery(payload).online
        file = next((directory/'inbox').glob('*.json'))
        envelope = bridge.read_record(file, versions=(1, 2))
        assert envelope['version'] == 2 and envelope['pi_delivery'] == 'steer'
        request['pi_delivery'] = 'follow-up'
        with self.assertRaisesRegex(cli.CallbackTransportBlocked, 'different delivery policy'):
            bridge.prepare_delivery(payload)
        assert bridge.read_record(file, versions=(1, 2)) == envelope
        cli.write_request(cli.ack_path(queue, 'fixture'), {'id': 'fixture'})
        cli.release_retained_target_lease(queue, request)

    def test_online_publication_retains_lease_and_never_spawns_pi(self):
        bound = self.bound
        tmp_path = self.directory
        request, _, directory, queue, payload = bound
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('online spawned Pi')):
            delivery = bridge.prepare_delivery(payload)
        assert delivery.online
        envelope = json.loads(next((directory / 'inbox').glob('*.json')).read_text())
        assert envelope['prompt'] == payload['prompt']
        assert cli.retained_target_lease_is_held(request)
        cli.write_request(cli.ack_path(queue, 'fixture'), {'id': 'fixture'})
        assert cli.wait_for_remote_delivery(payload, delivery, 1)['returncode'] == 0
        assert not cli.retained_target_lease_is_held(request)



    def test_online_timeout_remains_unknown_and_late_ack_reconciles(self):
        bound = self.bound
        tmp_path = self.directory
        request, _, _, queue, payload = bound
        delivery = bridge.prepare_delivery(payload)
        assert cli.wait_for_remote_delivery(payload, delivery, 0.01)['returncode'] == 125
        assert cli.retained_target_lease_is_held(request)
        cli.write_request(queue / 'failed/fixture.json', request)
        cli.write_request(cli.ack_path(queue, 'fixture'), {'id': 'fixture'})
        cli.reconcile_acknowledged_retained_leases(queue)
        assert not cli.retained_target_lease_is_held(request)



    def test_publication_failure_still_retains_unknown(self):
        bound = self.bound
        tmp_path = self.directory
        request, _, _, _, payload = bound
        original = bridge.storage.write_request
        def fail_envelope(path, data, **kwargs):
            if path.parent.name == 'inbox': raise OSError('disk full')
            return original(path, data, **kwargs)
        with mock.patch.object(bridge.storage, 'write_request', side_effect=fail_envelope):
            with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)
        assert cli.retained_target_lease_is_held(request)



    def test_offline_requires_managed_provenance_and_process_death(self):
        bound = self.bound
        tmp_path = self.directory
        request, owner, directory, _, payload = bound
        owner['owner_pid'] = 99999999; owner['state'] = 'closed'
        cli.write_request(directory / 'owner.json', owner)
        assert bridge.inspect_route(request)['status'] == 'blocked'
        with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)
        dead_managed(owner, directory)
        assert bridge.confirmed_owner_dead(owner)
        delivery = bridge.prepare_delivery(payload)
        assert not delivery.online
        assert delivery.command[-2:] == ['--extension', str(bridge.extension_path())]
        assert cli.acquire_path_lock(bridge.writer_lock_path(request['target']['value']), blocking=False) is None
        delivery.close()



    def test_alive_reused_pid_unknown_scope_and_permission_failure_never_authorize(self):
        bound = self.bound
        tmp_path = self.directory
        _, owner, directory, _, _ = bound
        dead_managed(owner, directory)
        owner['owner_pid'] = os.getpid()
        assert not bridge.confirmed_owner_dead(owner)
        owner['owner_pid'] = 99999999
        owner['owner_identity']['machine_id'] = 'another-host'
        assert not bridge.confirmed_owner_dead(owner)
        owner['owner_identity'] = cli.daemon_process_identity(os.getpid())
        if os.name != 'nt':
            with mock.patch.object(os, 'kill', side_effect=PermissionError('unknown')):
                assert not bridge.confirmed_owner_dead(owner)
        with mock.patch.object(cli, 'daemon_process_identity', return_value=None):
            assert not bridge.confirmed_owner_dead(owner)
        owner['owner_identity'] = {}
        assert not bridge.confirmed_owner_dead(owner)



    def test_offline_publication_barrier_and_failed_retention_preserve_old_owner(self):
        bound = self.bound
        tmp_path = self.directory
        request, owner, directory, queue, payload = bound
        bridge.prepare_delivery(payload)
        dead_managed(owner, directory)
        with self.assertRaisesRegex(cli.CallbackTransportBlocked, 'already published'):
            bridge.prepare_delivery(payload)
        for path in (directory / 'inbox').glob('*.json'): path.unlink()
        with mock.patch.object(cli, 'retain_target_lease', side_effect=ValueError('conflicting lease')):
            with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)
        assert bridge.read_record(directory / 'owner.json') == owner



    def test_frozen_profile_and_managed_orphan_reject(self):
        bound = self.bound
        tmp_path = self.directory
        request, owner, directory, _, payload = bound
        os.environ['PI_CODING_AGENT_DIR'] = '/different/current/profile'
        assert bridge.owner_record(request)[1] == owner
        owner['profile_dir'] = '/another/profile'; cli.write_request(directory / 'owner.json', owner)
        with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)
        owner['profile_dir'] = request['pi_profile_dir']
        owner.update(managed=True, lease_pid=99999999, lease_identity=cli.daemon_process_identity(os.getpid()))
        cli.write_request(directory / 'owner.json', owner)
        assert bridge.inspect_route(request)['status'] == 'blocked'



    def test_aliases_share_writer_and_target_locks_and_partial_ids_reject(self):
        bound = self.bound
        tmp_path = self.directory
        request, _, _, _, _ = bound
        alias = tmp_path / 'alias.jsonl'
        try: alias.symlink_to(request['target']['value'])
        except OSError: self.skipTest('symlink privilege unavailable')
        other = dict(request, target={'kind': 'session', 'value': str(alias)})
        assert cli.target_lock_path(other) == cli.target_lock_path(request)
        assert bridge.writer_lock_path(str(alias)) == bridge.writer_lock_path(request['target']['value'])
        for invalid in ('partial-id', '', None):
            with self.assertRaises(ValueError): bridge.canonical_session(invalid)



    def test_queue_ids_are_scoped_and_legacy_routes_blocked(self):
        bound = self.bound
        tmp_path = self.directory
        request, _, directory, _, payload = bound
        bridge.prepare_delivery(payload)
        queue2 = tmp_path / 'queue2'; cli.ensure_daemon_dirs(queue2)
        # A different queue must never overwrite the first retained target lease.
        with self.assertRaises(RuntimeError): bridge.prepare_delivery(dict(payload, queue_dir=str(queue2)))
        assert len(list((directory / 'inbox').glob('*.json'))) == 1
        legacy = dict(request); legacy.pop('pi_callback_protocol')
        assert bridge.inspect_route(legacy)['status'] == 'blocked'
        assert callback_transport.inspect_route(dict(legacy, callback_mode='manual'))['status'] == 'manual'



    def test_installer_preserves_foreign_extension_and_settings(self):
        bound = self.bound
        tmp_path = self.directory
        profile = tmp_path / 'isolated-install'
        profile.mkdir(); (profile / 'settings.json').write_text('{"personal":"keep"}')
        args = argparse.Namespace(profile=str(profile), force=False)
        assert bridge.install_extension(args) == 0
        target = profile / 'extensions/ltc-callback.js'; target.write_text('// user edits\n')
        with self.assertRaisesRegex(ValueError, 'Preserving'): bridge.install_extension(args)
        assert target.read_text() == '// user edits\n'
        assert (profile / 'settings.json').read_text() == '{"personal":"keep"}'



    def test_launcher_rejects_session_overrides_before_spawn(self):
        bound = self.bound
        for option in ['--session', '--session=x', '--continue', '-c', '--resume', '-r', '--fork', '--no-session', '--session-dir']:
            with self.subTest(option=option):
                request, _, _, _, _ = bound
                args = argparse.Namespace(cwd=request['cwd'], session=request['target']['value'], pi_args=[option])
                with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('spawned')):
                    with self.assertRaises(ValueError): bridge.managed_pi(args)



    def test_new_launcher_rejects_previous_managed_owner_with_unknown_scope(self):
        bound = self.bound
        tmp_path = self.directory
        request,owner,directory,_,_=bound
        dead_managed(owner,directory); owner['owner_identity']['machine_id']='foreign-machine'
        cli.write_request(directory/'owner.json',owner)
        args=argparse.Namespace(cwd=request['cwd'],session=request['target']['value'],pi_args=[])
        with mock.patch.object(subprocess,'Popen',side_effect=AssertionError('spawned')):
            with self.assertRaisesRegex(ValueError, 'Cannot confirm'): bridge.managed_pi(args)
        assert bridge.read_record(directory/'owner.json')==owner



    def test_definite_pre_spawn_failure_restores_owner_and_target_lease(self):
        bound = self.bound
        tmp_path = self.directory
        request,owner,directory,_,payload=bound
        dead_managed(owner,directory)
        plan=bridge.prepare_delivery(payload)
        assert bridge.read_record(directory/'owner.json')['state']=='reserved'
        # No subprocess was created; close is the definite pre-spawn rollback.
        plan.close()
        assert bridge.read_record(directory/'owner.json')==owner
        assert not cli.retained_target_lease_is_held(request)



    def test_managed_child_strips_reservation_and_callback_cancel_paths(self):
        bound = self.bound
        tmp_path = self.directory
        values={'LTC_PI_RESERVATION':'owner','LTC_PI_RECOVERY':'1',
                'LTC_PI_CALLBACK_ACK_PATH':'/ack','LTC_PI_CALLBACK_CANCEL_PATH':'/cancel',
                'PI_CODING_AGENT_DIR':'/keep/profile','PATH':'/keep/path'}
        assert cli.child_agent_environment(values)=={'PI_CODING_AGENT_DIR':'/keep/profile','PATH':'/keep/path'}
