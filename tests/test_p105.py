"""P105 selection, private boot assets and handoff boundaries without hardware."""
from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from orchard_cli import devices, releases, service
from orchard_cli.contracts import Adapter, Connection
from orchard_cli.p105 import adapter, boot, network, usb
from orchard_cli.progress import Progress
from orchard_cli.protocol import DeviceError
from orchard_cli.state import BootState, DeviceState

BOOT = '11111111-1111-4111-8111-111111111111'


class P105(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, ORCHARD_HOME=str(self.root/'store'))
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_registered_adapter_import_has_no_device_or_config_access(self):
        with patch.object(adapter, 'discover', side_effect=AssertionError('USB access')), \
                patch('orchard_cli.p105.config.settings', side_effect=AssertionError('private read')):
            self.assertIs(devices.load('p105'), adapter)
            self.assertIsInstance(adapter, Adapter)

    def test_explicit_install_profile_is_preserved_before_confirmation(self):
        implementation=Mock(PROFILE='p105',NAME='iPad mini')
        with patch.object(service,'choose',return_value=(implementation,{'mode':'ios'})) as selected:
            result=service.install('unused',profile='p105',input_fn=lambda _:'n',progress=Progress(io.StringIO()))
        self.assertTrue(result['cancelled'])
        selected.assert_called_once_with('p105')
        implementation.boot.assert_not_called()

    def test_firmware_inputs_are_verified_before_returning_the_cache(self):
        payload=self.root/'firmware';payload.mkdir();blob=payload/'blob';blob.write_bytes(b'firmware')
        recipe=self.root/'recipe.json';recipe.write_text(json.dumps({'files':[{'name':'test',
            'destination':'blob','sha256':hashlib.sha256(b'firmware').hexdigest()}],'generated':[]}))
        with patch.object(adapter,'settings',return_value={'firmware_root':str(payload)}), \
                patch.object(adapter,'firmware_path',return_value=recipe):
            self.assertEqual(adapter.firmware_inputs(Mock(),Mock(),allow_download=False),payload)
            blob.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'pinned recipe'):adapter.firmware_inputs(Mock(),Mock())

    def test_discovery_refuses_multiple_p105_devices(self):
        one = {'mode':'ios','location':'one','ecid':1,'udid':'1'*40}
        with patch.object(adapter, 'list_devices', return_value=[one,{**one,'ecid':2}]):
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                adapter.discover()

    def test_unrelated_ios_product_is_rejected(self):
        import plistlib
        data = {'ProductType':'iPhone10,6','HardwareModel':'d221ap','UniqueDeviceID':'1'*40,'UniqueChipID':1}
        with patch.object(usb, 'tool', return_value='ideviceinfo'), \
                patch.object(usb, 'query', return_value=plistlib.dumps(data).decode()):
            with self.assertRaisesRegex(ValueError, 'iPad2,5'):
                usb.ios('1'*40)

    def test_ios_discovery_uses_connected_usb_identifiers(self):
        observed=[{'mode':'other','udid':'1'*40,'product':'iPad'},
                  {'mode':'other','udid':'2'*40,'product':'iPhone'}]
        ipad={'mode':'ios','ecid':1,'udid':'1'*40}
        def query(identifier):
            if identifier=='1'*40:return ipad
            raise ValueError('unrelated iPhone')
        with patch.object(usb.apple_usb,'devices',return_value=observed),patch.object(usb,'ios',side_effect=query):
            self.assertEqual(usb.observations(),[ipad])

    def test_recovery_requires_one_identified_serial_endpoint(self):
        row = {'mode':'linux','product':'Orchard-P105','ecid':1,'boot_id':BOOT,'serial_port':'/dev/ttyACM0'}
        with patch.object(usb.apple_usb, 'devices', return_value=[row,row]):
            with self.assertRaises(ValueError):usb.recovery()
        with patch.object(usb.apple_usb, 'devices', return_value=[{**row,'boot_id':None}]):
            with self.assertRaisesRegex(ValueError, 'identity'):usb.recovery()

    def test_ibec_query_is_targeted_and_rejects_changed_ecid(self):
        text='PRODUCT: iPad2,5\nMODE: Recovery\nECID: 0x2\n'
        with patch.object(usb, 'tool', return_value='irecovery'), patch.object(usb, 'query', return_value=text) as query:
            with self.assertRaisesRegex(ValueError, 'identity changed'):usb.ibec(1)
            self.assertEqual(query.call_args.args,('irecovery','-i','1','-q'))

    def test_wrong_private_target_is_refused_before_state_or_transport(self):
        with patch.object(boot, 'ios_connection') as ios, patch.object(boot, 'upload') as upload:
            with self.assertRaisesRegex(ValueError, 'another device'):
                boot.boot({'mode':'ios','ecid':1}, {}, {'target_ecid':2,'assets':{}}, Mock())
            ios.assert_not_called();upload.assert_not_called()
        self.assertFalse((self.root/'store').exists())

    def test_uncertain_upload_is_never_retried(self):
        result = subprocess.CompletedProcess([],0,stdout=b'99%',stderr=b'')
        with patch.object(boot.usb,'ibec') as selected, patch.object(boot,'tool',return_value='irecovery'), \
                patch.object(boot.subprocess,'run',return_value=result) as send:
            with self.assertRaisesRegex(RuntimeError,'unknown'):
                boot.upload({'ecid':1},Path('bundle'))
            selected.assert_called_once_with(1)
            send.assert_called_once()

    def test_upload_timeout_propagates_without_retry(self):
        with patch.object(boot.usb,'ibec'), patch.object(boot,'tool',return_value='irecovery'), \
                patch.object(boot.subprocess,'run',side_effect=subprocess.TimeoutExpired('irecovery',180)) as send:
            with self.assertRaises(subprocess.TimeoutExpired):boot.upload({'ecid':1},Path('bundle'))
            send.assert_called_once()

    def test_saved_ibec_wait_continues_before_either_upload(self):
        folder=self.root/'attempts';folder.mkdir()
        receipt=folder/'wait.json';manifest={'target_ecid':1,'assets':{'recovery':{'sha256':'a'*64}}}
        record={'ecid':1,'assets':manifest['assets'],'phase':'awaiting-ibec'}
        boot.save(receipt,record)
        with patch.object(boot.time,'time',return_value=receipt.stat().st_mtime+1):
            path,result=boot.pending_attempt(folder,{'ecid':1},manifest)
        self.assertEqual(path,receipt);self.assertEqual(result,record)

    def test_saved_wait_rejects_uncertain_upload_changed_assets_or_stale_state(self):
        folder=self.root/'attempts';folder.mkdir();receipt=folder/'wait.json'
        manifest={'target_ecid':1,'assets':{'recovery':{'sha256':'a'*64}}}
        base={'ecid':1,'assets':manifest['assets'],'phase':'awaiting-ibec'}
        for change in ({'phase':'recovery-upload-outcome-unknown'},{'ecid':2},{'assets':{}},{}):
            boot.save(receipt,{**base,**change})
            age=901 if not change else 1
            with self.subTest(change=change),patch.object(boot.time,'time',return_value=receipt.stat().st_mtime+age):
                with self.assertRaisesRegex(ValueError,'latest verified'):boot.pending_attempt(folder,{'ecid':1},manifest)

    def test_ibec_continuation_uses_one_saved_attempt_and_each_payload_once(self):
        from orchard_cli.state import private_directory
        selected={'mode':'ibec','ecid':1,'location':'usb','resumed':False}
        assets={'recovery':self.root/'bundle','loader':self.root/'loader'}
        manifest={'target_ecid':1,'assets':{'recovery':{'sha256':'a'*64}}}
        folder=private_directory(DeviceState('p105',1).root/'boot-attempts');receipt=folder/'wait.json'
        boot.save(receipt,{'ecid':1,'assets':manifest['assets'],'phase':'awaiting-ibec'})
        endpoint={'mode':'linux','ecid':1,'boot_id':BOOT,'location':'usb','serial_port':'/dev/ttyACM0'}
        phases=[]
        def send(device,path):phases.append(json.loads(receipt.read_text())['phase'])
        with patch.object(boot,'verify_assets'),patch.object(boot,'ios_connection') as ios, \
                patch.object(usb,'ibec'),patch.object(usb,'recovery',return_value=endpoint), \
                patch.object(boot,'upload',side_effect=send) as upload:
            boot.boot(selected,assets,manifest,Mock())
        ios.assert_not_called();self.assertEqual(upload.call_count,2)
        self.assertEqual(phases,['recovery-upload-outcome-unknown','loader-upload-outcome-unknown'])
        self.assertEqual(json.loads(receipt.read_text())['phase'],'complete')
        self.assertEqual(selected['boot_id'],BOOT)

    def test_running_recovery_is_observed_without_upload(self):
        selected={'mode':'linux','ecid':1,'boot_id':BOOT,'resumed':True}
        with patch.object(usb,'recovery',return_value=selected), patch.object(boot,'upload') as upload:
            boot.boot(selected,{}, {'target_ecid':1,'assets':{}},Mock())
            upload.assert_not_called()

    def test_running_recovery_rejects_another_devices_manifest(self):
        selected={'mode':'linux','ecid':1,'boot_id':BOOT,'resumed':True}
        with patch.object(usb,'recovery') as query:
            with self.assertRaisesRegex(ValueError,'another device'):
                boot.boot(selected,{}, {'target_ecid':2,'assets':{}},Mock())
            query.assert_not_called()

    def test_macos_product_spelling_keeps_the_same_identity_checks(self):
        row={'mode':'linux','product':'Orchard_P105','ecid':1,'boot_id':BOOT,'serial_port':'/dev/ttyACM0'}
        with patch.object(usb.apple_usb,'devices',return_value=[row]):
            self.assertEqual(usb.recovery(),row)
        with patch.object(usb.apple_usb,'devices',return_value=[{**row,'boot_id':None}]):
            with self.assertRaisesRegex(ValueError,'identity'):usb.recovery()

    def test_local_p105_manifest_resolves_without_pongo(self):
        rows=['schema=1','kind="orchard.boot"','device="p105"','boot_method="jailbreak-kdfu"',
              'min_orchard="0.1.0"','protocol=1','private=true','target_ecid=1']
        for role in ('boot','loader','ibec','kloader'):
            data=role.encode();(self.root/role).write_bytes(data)
            rows += ['['+role+']','file="'+role+'"','bytes='+str(len(data)),
                     'sha256="'+hashlib.sha256(data).hexdigest()+'"']
        manifest=self.root/'boot.toml';manifest.write_text('\n'.join(rows)+'\n')
        record,assets=releases.resolve(manifest,'p105',purpose='boot')
        self.assertEqual(set(assets),{'recovery','loader','ibec','kloader'})
        self.assertEqual(record['target_ecid'],1)
        with self.assertRaises(ValueError):releases.resolve(manifest,'p105',purpose='install')
        manifest.write_text(manifest.read_text().replace('private=true','private=false'))
        with self.assertRaises(ValueError):releases.resolve(manifest,'p105',purpose='boot')

    def test_nfs_root_rejects_remote_server_and_shell_or_path_tricks(self):
        for source in ('other:/root','10.55.0.1:/','10.55.0.1:/root\nother',
                       '10.55.0.1:/root/../else','10.55.0.1:/root;true'):
            with self.subTest(source=source),self.assertRaises(ValueError):network.nfs_root(source)

    def test_existing_host_address_is_retained_during_cleanup(self):
        scope=BootState(DeviceState('p105',1),BOOT)
        with patch.object(usb,'recovery',return_value={'ecid':1,'boot_id':BOOT,'network_interface':'en9','network_mac':network.HOST_MAC}), \
                patch.object(network,'interfaces',return_value={'en9':'ether '+network.HOST_MAC+' inet 10.55.0.1'}), \
                patch.object(network,'privileged') as admin:
            path=network.ensure(scope)
            self.assertFalse(json.loads(path.read_text())['changed'])
            self.assertTrue(network.cleanup(path)['cleaned'])
            admin.assert_not_called()

    def test_selected_usb_registry_link_survives_masked_ifconfig_mac(self):
        scope=BootState(DeviceState('p105',1),BOOT)
        endpoint={'ecid':1,'boot_id':BOOT,'network_interface':'en9','network_mac':network.HOST_MAC}
        with patch.object(usb,'recovery',return_value=endpoint), \
                patch.object(network,'interfaces',return_value={'en9':'ether 02:00:00:00:00:00 inet 10.55.0.1'}) as query, \
                patch.object(network,'privileged') as admin:
            record=json.loads(network.ensure(scope).read_text())
        query.assert_called_with('en9')
        admin.assert_not_called()
        self.assertFalse(record['changed'])

    def test_macos_network_metadata_belongs_to_the_usb_parent(self):
        import plistlib
        def gadget(ecid,name,interface,port):
            return {'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x4142,
                    'locationID':ecid,'USB Serial Number':f'ECID:{ecid:x} BOOT:{BOOT}',
                    'USB Product Name':name,'IORegistryEntryChildren':[
                        {'IOObjectClass':'IOSerialBSDClient','IOCalloutDevice':port},
                        {'IOObjectClass':'IOUserNetworkEthernet','IOMACAddress':bytes.fromhex('02105a050001'),
                         'IORegistryEntryChildren':[{'IOObjectClass':'IOSkywalkLegacyEthernetInterface','BSD Name':interface}]}]}
        raw=plistlib.dumps([{'IOObjectClass':'IOUSBHostDevice','idVendor':0x05ac,'idProduct':0x800c,
            'locationID':99,'USB Product Name':'Hub','IORegistryEntryChildren':[
                gadget(1,'Orchard_P105','en9','/dev/cu.p105'),gadget(2,'Other','en10','/dev/cu.other')]}])
        with patch.object(usb.apple_usb.sys,'platform','darwin'),patch.object(usb.apple_usb.subprocess,'run',
                return_value=subprocess.CompletedProcess([],0,raw,b'')):
            rows=usb.apple_usb.devices()
        selected=next(r for r in rows if r['product']=='Orchard_P105')
        self.assertEqual(selected['network_interface'],'en9')
        self.assertEqual(selected['serial_port'],'/dev/cu.p105')
        self.assertNotIn('network_interface',next(r for r in rows if r['product']=='Hub'))

    def test_linux_network_metadata_comes_from_that_devices_interface(self):
        from orchard_cli.apple import linux_usb
        node=self.root/'1-2';node.mkdir()
        for name,value in {'idVendor':'05ac','idProduct':'4142','product':'Orchard-P105',
                           'serial':f'ECID:1 BOOT:{BOOT}'}.items():(node/name).write_text(value)
        net=node/'1-2:1.0/net/usb0';net.mkdir(parents=True);(net/'address').write_text(network.HOST_MAC)
        nested=node/'1-2.1/1-2.1:1.0/net/wrong';nested.mkdir(parents=True)
        (nested/'address').write_text('aa:bb:cc:dd:ee:ff')
        rows=linux_usb.devices(self.root)
        self.assertEqual(rows[0]['network_interface'],'usb0')
        self.assertEqual(rows[0]['network_mac'],network.HOST_MAC)

    def test_cleanup_refuses_an_attached_p105_even_if_selection_is_ambiguous(self):
        scope=BootState(DeviceState('p105',1),BOOT)
        path=scope.root/'network.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'profile':'p105','ecid':1,'boot_id':BOOT,'mac':network.HOST_MAC,
                                   'phase':'ready','interface':'en9','changed':True}));path.chmod(0o600)
        with patch.object(usb.apple_usb,'devices',return_value=[{'product':'Orchard_P105'},{'product':'Orchard-P105'}]), \
                patch.object(network,'privileged') as admin:
            with self.assertRaisesRegex(ValueError,'Shut down'):network.cleanup(path)
            admin.assert_not_called()

    def test_handoff_uncertainty_is_saved_and_never_replayed(self):
        scope=BootState(DeviceState('p105',1),BOOT)
        connection=Connection([],scope,Mock())
        client=Mock()
        client.call.side_effect=[{'profile':'p105','ecid':1,'boot_id':BOOT,'capabilities':['handoff-nfs']},DeviceError('lost response')]
        with patch.object(service,'Client',return_value=client),patch.object(network,'check_export'):
            with self.assertRaises(DeviceError):service.handoff_nfs(connection,'10.55.0.1:/root',Mock())
        report=json.loads((scope.root/'last-nfs-handoff.json').read_text())
        self.assertEqual(report['status'],'unknown')
        self.assertEqual(client.call.call_count,2)
        client.call.side_effect=None
        client.call.return_value={'profile':'p105','ecid':1,'boot_id':BOOT,'capabilities':['handoff-nfs']}
        with patch.object(service,'Client',return_value=client),patch.object(network,'check_export'):
            with self.assertRaisesRegex(ValueError,'already attempted'):
                service.handoff_nfs(connection,'10.55.0.1:/root',Mock())
        self.assertEqual(client.call.call_count,3)


if __name__ == '__main__':unittest.main()
