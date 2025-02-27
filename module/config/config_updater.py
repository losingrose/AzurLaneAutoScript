import re
from copy import deepcopy

from cached_property import cached_property

from deploy.utils import DEPLOY_TEMPLATE, poor_yaml_read, poor_yaml_write
from module.base.timer import timer
from module.config.redirect_utils.shop_filter import bp_redirect
from module.config.redirect_utils.utils import upload_redirect, api_redirect
from module.config.server import to_server, to_package, VALID_PACKAGE, VALID_CHANNEL_PACKAGE
from module.config.utils import *

CONFIG_IMPORT = '''
import datetime

# This file was automatically generated by module/config/config_updater.py.
# Don't modify it manually.


class GeneratedConfig:
    """
    Auto generated configuration
    """
'''.strip().split('\n')
ARCHIVES_PREFIX = {
    'cn': '档案 ',
    'en': 'archives ',
    'jp': '檔案 ',
    'tw': '檔案 '
}


class Event:
    def __init__(self, text):
        self.date, self.directory, self.name, self.cn, self.en, self.jp, self.tw \
            = [x.strip() for x in text.strip('| \n').split('|')]

        self.directory = self.directory.replace(' ', '_')
        self.cn = self.cn.replace('、', '')
        self.en = self.en.replace(',', '').replace('\'', '').replace('\\', '')
        self.jp = self.jp.replace('、', '')
        self.tw = self.tw.replace('、', '')
        self.is_war_archives = self.directory.startswith('war_archives')
        self.is_raid = self.directory.startswith('raid_')
        for server in ARCHIVES_PREFIX.keys():
            if self.__getattribute__(server) == '-':
                self.__setattr__(server, None)
            else:
                if self.is_war_archives:
                    self.__setattr__(server, ARCHIVES_PREFIX[server] + self.__getattribute__(server))

    def __str__(self):
        return self.directory

    def __eq__(self, other):
        return str(self) == str(other)


class ConfigGenerator:
    @cached_property
    def argument(self):
        """
        Load argument.yaml, and standardise its structure.

        <group>:
            <argument>:
                type: checkbox|select|textarea|input
                value:
                option (Optional): Options, if argument has any options.
                validate (Optional): datetime
        """
        data = {}
        raw = read_file(filepath_argument('argument'))
        for path, value in deep_iter(raw, depth=2):
            arg = {
                'type': 'input',
                'value': '',
                # option
            }
            if not isinstance(value, dict):
                value = {'value': value}
            arg['type'] = data_to_type(value, arg=path[1])
            if isinstance(value['value'], datetime):
                arg['validate'] = 'datetime'
            # Manual definition has the highest priority
            arg.update(value)
            deep_set(data, keys=path, value=arg)

        return data

    @cached_property
    def task(self):
        """
        <task>:
            - <group>
        """
        return read_file(filepath_argument('task'))

    @cached_property
    def default(self):
        """
        <task>:
            <group>:
                <argument>: value
        """
        return read_file(filepath_argument('default'))

    @cached_property
    def override(self):
        """
        <task>:
            <group>:
                <argument>: value
        """
        return read_file(filepath_argument('override'))

    @cached_property
    def gui(self):
        """
        <i18n_group>:
            <i18n_key>: value, value is None
        """
        return read_file(filepath_argument('gui'))

    @cached_property
    @timer
    def args(self):
        """
        Merge definitions into standardised json.

            task.yaml ---+
        argument.yaml ---+-----> args.json
        override.yaml ---+
         default.yaml ---+

        """
        # Construct args
        data = {}
        for task, groups in self.task.items():
            for group in groups:
                if group not in self.argument:
                    print(f'`{task}.{group}` is not related to any argument group')
                    continue
                deep_set(data, keys=[task, group], value=deepcopy(self.argument[group]))

        def check_override(path, value):
            # Check existence
            old = deep_get(data, keys=path, default=None)
            if old is None:
                print(f'`{".".join(path)}` is not a existing argument')
                return False
            # Check type
            # But allow `Interval` to be different
            old_value = old.get('value', None) if isinstance(old, dict) else old
            value = old.get('value', None) if isinstance(value, dict) else value
            if type(value) != type(old_value) and path[2] not in ['SuccessInterval', 'FailureInterval']:
                print(
                    f'`{value}` ({type(value)}) and `{".".join(path)}` ({type(old_value)}) are in different types')
                return False
            # Check option
            if isinstance(old, dict) and 'option' in old:
                if value not in old['option']:
                    print(f'`{value}` is not an option of argument `{".".join(path)}`')
                    return False
            return True

        # Set defaults
        for p, v in deep_iter(self.default, depth=3):
            if not check_override(p, v):
                continue
            deep_set(data, keys=p + ['value'], value=v)
        # Override non-modifiable arguments
        for p, v in deep_iter(self.override, depth=3):
            if not check_override(p, v):
                continue
            if isinstance(v, dict):
                deep_default(v, keys='type', value='hide')
                for arg_k, arg_v in v.items():
                    deep_set(data, keys=p + [arg_k], value=arg_v)
            else:
                deep_set(data, keys=p + ['value'], value=v)
                deep_set(data, keys=p + ['type'], value='hide')
        # Set command
        for task in self.task.keys():
            if deep_get(data, keys=f'{task}.Scheduler.Command'):
                deep_set(data, keys=f'{task}.Scheduler.Command.value', value=task)
                deep_set(data, keys=f'{task}.Scheduler.Command.type', value='hide')

        return data

    @timer
    def generate_code(self):
        """
        Generate python code.

        args.json ---> config_generated.py

        """
        visited_group = set()
        visited_path = set()
        lines = CONFIG_IMPORT
        for path, data in deep_iter(self.argument, depth=2):
            group, arg = path
            if group not in visited_group:
                lines.append('')
                lines.append(f'    # Group `{group}`')
                visited_group.add(group)

            option = ''
            if 'option' in data and data['option']:
                option = '  # ' + ', '.join([str(opt) for opt in data['option']])
            path = '.'.join(path)
            lines.append(f'    {path_to_arg(path)} = {repr(parse_value(data["value"], data=data))}{option}')
            visited_path.add(path)

        with open(filepath_code(), 'w', encoding='utf-8', newline='') as f:
            for text in lines:
                f.write(text + '\n')

    @timer
    def generate_i18n(self, lang):
        """
        Load old translations and generate new translation file.

                     args.json ---+-----> i18n/<lang>.json
        (old) i18n/<lang>.json ---+

        """
        new = {}
        old = read_file(filepath_i18n(lang))

        def deep_load(keys, default=True, words=('name', 'help')):
            for word in words:
                k = keys + [str(word)]
                d = ".".join(k) if default else str(word)
                v = deep_get(old, keys=k, default=d)
                deep_set(new, keys=k, value=v)

        # Menu
        for path, data in deep_iter(self.menu, depth=2):
            func, group = path
            deep_load(['Menu', func])
            deep_load(['Menu', group])
            for task in data:
                deep_load([func, task])
        # Arguments
        visited_group = set()
        for path, data in deep_iter(self.argument, depth=2):
            if path[0] not in visited_group:
                deep_load([path[0], '_info'])
                visited_group.add(path[0])
            deep_load(path)
            if 'option' in data:
                deep_load(path, words=data['option'], default=False)
        # Event names
        # Names come from SameLanguageServer > en > cn > jp > tw
        events = {}
        for event in self.event:
            if lang in LANG_TO_SERVER:
                name = event.__getattribute__(LANG_TO_SERVER[lang])
                if name:
                    deep_default(events, keys=event.directory, value=name)
        for server in ['en', 'cn', 'jp', 'tw']:
            for event in self.event:
                name = event.__getattribute__(server)
                if name:
                    deep_default(events, keys=event.directory, value=name)
        for event in self.event:
            name = events.get(event.directory, event.directory)
            deep_set(new, keys=f'Campaign.Event.{event.directory}', value=name)
        # Package names
        for package, server in VALID_PACKAGE.items():
            path = ['Emulator', 'PackageName', package]
            if deep_get(new, keys=path) == package:
                deep_set(new, keys=path, value=server.upper())

        for package, server_and_channel in VALID_CHANNEL_PACKAGE.items():
            server, channel = server_and_channel
            name = deep_get(new, keys=['Emulator', 'PackageName', to_package(server)])
            if lang == SERVER_TO_LANG[server]:
                value = f'{name} {channel}渠道服 {package}'
            else:
                value = f'{name} {package}'
            deep_set(new, keys=['Emulator', 'PackageName', package], value=value)
        # GUI i18n
        for path, _ in deep_iter(self.gui, depth=2):
            group, key = path
            deep_load(keys=['Gui', group], words=(key,))

        write_file(filepath_i18n(lang), new)

    @cached_property
    def menu(self):
        """
        Generate menu definitions

        task.yaml --> menu.json

        """
        data = {}

        # Task menu
        group = ''
        tasks = []
        with open(filepath_argument('task'), 'r', encoding='utf-8') as f:
            for line in f.readlines():
                line = line.strip('\n')
                if '=====' in line:
                    if tasks:
                        deep_set(data, keys=f'Task.{group}', value=tasks)
                    group = line.strip('#=- ')
                    tasks = []
                if group:
                    if line.endswith(':'):
                        tasks.append(line.strip('\n=-#: '))
        if tasks:
            deep_set(data, keys=f'Task.{group}', value=tasks)

        return data

    @cached_property
    @timer
    def event(self):
        """
        Returns:
            list[Event]: From latest to oldest
        """
        events = []
        with open('./campaign/Readme.md', encoding='utf-8') as f:
            for text in f.readlines():
                if re.search('\d{8}', text):
                    event = Event(text)
                    events.append(event)

        return events[::-1]

    def insert_event(self):
        """
        Insert event information into `self.args`.

        ./campaign/Readme.md -----+
                                  v
                   args.json -----+-----> args.json
        """
        for event in self.event:
            for server in ARCHIVES_PREFIX.keys():
                name = event.__getattribute__(server)

                def insert(key):
                    opts = deep_get(self.args, keys=f'{key}.Campaign.Event.option')
                    if event not in opts:
                        opts.append(event)
                    if name:
                        deep_default(self.args, keys=f'{key}.Campaign.Event.{server}', value=event)

                if name:
                    if event.is_raid:
                        insert('Raid')
                        insert('RaidDaily')
                    elif event.is_war_archives:
                        insert('WarArchives')
                    else:
                        insert('Event')
                        insert('Event2')
                        insert('EventAb')
                        insert('EventCd')
                        insert('EventSp')
                        insert('GemsFarming')

        # Remove campaign_main from event list
        for task in ['Event', 'Event2', 'EventAb', 'EventCd', 'EventSp', 'Raid', 'RaidDaily', 'WarArchives']:
            options = deep_get(self.args, keys=f'{task}.Campaign.Event.option')
            options = [option for option in options if option != 'campaign_main']
            deep_set(self.args, keys=f'{task}.Campaign.Event.option', value=options)

    @staticmethod
    def generate_deploy_template():
        template = poor_yaml_read(DEPLOY_TEMPLATE)
        cn = {
            'Repository': 'https://gitee.com/LmeSzinc/AzurLaneAutoScript',
            'PypiMirror': 'https://pypi.tuna.tsinghua.edu.cn/simple',
        }
        aidlux = {
            'GitExecutable': '/usr/bin/git',
            'PythonExecutable': '/usr/bin/python',
            'RequirementsFile': './deploy/AidLux/0.92/requirements.txt',
            'AdbExecutable': '/usr/bin/adb',
        }

        def update(suffix, *args):
            file = f'./config/deploy.{suffix}.yaml'
            new = deepcopy(template)
            for dic in args:
                new.update(dic)
            poor_yaml_write(data=new, file=file)

        update('template')
        update('template-cn', cn)
        update('template-AidLux', aidlux)
        update('template-AidLux-cn', aidlux, cn)

    def insert_package(self):
        option = deep_get(self.argument, keys='Emulator.PackageName.option')
        option += list(VALID_PACKAGE.keys())
        option += list(VALID_CHANNEL_PACKAGE.keys())
        deep_set(self.argument, keys='Emulator.PackageName.option', value=option)
        deep_set(self.args, keys='Alas.Emulator.PackageName.option', value=option)

    @timer
    def generate(self):
        _ = self.args
        _ = self.menu
        _ = self.event
        self.insert_event()
        self.insert_package()
        write_file(filepath_args(), self.args)
        write_file(filepath_args('menu'), self.menu)
        self.generate_code()
        for lang in LANGUAGES:
            self.generate_i18n(lang)
        self.generate_deploy_template()


class ConfigUpdater:
    # source, target, (optional)convert_func
    redirection = [
        ('OpsiDaily.OpsiDaily.BuySupply', 'OpsiShop.Scheduler.Enable'),
        ('OpsiDaily.Scheduler.Enable', 'OpsiDaily.OpsiDaily.DoMission'),
        ('OpsiShop.Scheduler.Enable', 'OpsiShop.OpsiShop.BuySupply'),
        ('ShopOnce.GuildShop.Filter', 'ShopOnce.GuildShop.Filter', bp_redirect),
        ('ShopOnce.MedalShop.Filter', 'ShopOnce.MedalShop.Filter', bp_redirect),
        (('Alas.DropRecord.SaveResearch', 'Alas.DropRecord.UploadResearch'),
         'Alas.DropRecord.ResearchRecord', upload_redirect),
        (('Alas.DropRecord.SaveCommission', 'Alas.DropRecord.UploadCommission'),
         'Alas.DropRecord.CommissionRecord', upload_redirect),
        (('Alas.DropRecord.SaveOpsi', 'Alas.DropRecord.UploadOpsi'),
         'Alas.DropRecord.OpsiRecord', upload_redirect),
        (('Alas.DropRecord.SaveMeowfficerTalent', 'Alas.DropRecord.UploadMeowfficerTalent'),
         'Alas.DropRecord.MeowfficerTalent', upload_redirect),
        ('Alas.DropRecord.SaveCombat', 'Alas.DropRecord.CombatRecord', upload_redirect),
        ('Alas.DropRecord.SaveMeowfficer', 'Alas.DropRecord.MeowfficerBuy', upload_redirect),
        ('Alas.Emulator.PackageName', 'Alas.DropRecord.API', api_redirect)
    ]

    @cached_property
    def args(self):
        return read_file(filepath_args())

    def config_update(self, old, is_template=False):
        """
        Args:
            old (dict):
            is_template (bool):

        Returns:
            dict:
        """
        new = {}

        def deep_load(keys):
            data = deep_get(self.args, keys=keys, default={})
            value = deep_get(old, keys=keys, default=data['value'])
            if value is None or value == '' or data['type'] in ['disable', 'hide'] or is_template:
                value = data['value']
            value = parse_value(value, data=data)
            deep_set(new, keys=keys, value=value)

        for path, _ in deep_iter(self.args, depth=3):
            deep_load(path)

        # AzurStatsID
        if is_template:
            deep_set(new, 'Alas.DropRecord.AzurStatsID', None)
        else:
            deep_default(new, 'Alas.DropRecord.AzurStatsID', random_id())
        # Update to latest event
        server = to_server(deep_get(new, 'Alas.Emulator.PackageName', 'cn'))
        if not is_template:
            for task in ['Event', 'Event2', 'EventAb', 'EventCd', 'EventSp', 'Raid', 'RaidDaily']:
                deep_set(new,
                         keys=f'{task}.Campaign.Event',
                         value=deep_get(self.args, f'{task}.Campaign.Event.{server}'))
            for task in ['GemsFarming']:
                if deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') != 'campaign_main':
                    deep_set(new,
                             keys=f'{task}.Campaign.Event',
                             value=deep_get(self.args, f'{task}.Campaign.Event.{server}'))
        # War archive does not allow campaign_main
        for task in ['WarArchives']:
            if deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') == 'campaign_main':
                deep_set(new,
                         keys=f'{task}.Campaign.Event',
                         value=deep_get(self.args, f'{task}.Campaign.Event.{server}'))

        if not is_template:
            new = self.config_redirect(old, new)

        return new

    def config_redirect(self, old, new):
        """
        Convert old settings to the new.

        Args:
            old (dict):
            new (dict):

        Returns:
            dict:
        """
        for row in self.redirection:
            if len(row) == 2:
                source, target = row
                update_func = None
            elif len(row) == 3:
                source, target, update_func = row
            else:
                continue

            if isinstance(source, tuple):
                value = []
                error = False
                for attribute in source:
                    tmp = deep_get(old, keys=attribute, default=None)
                    if tmp is None:
                        error = True
                        continue
                    value.append(tmp)
                if error:
                    continue
            else:
                value = deep_get(old, keys=source, default=None)
                if value is None:
                    continue

            if update_func is not None:
                value = update_func(value)

            if isinstance(target, tuple):
                for i in range(0, len(target)):
                    if deep_get(old, keys=target[i], default=None) is None:
                        deep_set(new, keys=target[i], value=value[i])
            elif deep_get(old, keys=target, default=None) is None:
                deep_set(new, keys=target, value=value)

        return new

    def read_file(self, config_name):
        """
        Read and update config file.

        Args:
            config_name (str): ./config/{file}.json

        Returns:
            dict:
        """
        old = read_file(filepath_config(config_name))
        return self.config_update(old, is_template=config_name == 'template')

    @staticmethod
    def write_file(config_name, data):
        """
        Write config file.

        Args:
            config_name (str): ./config/{file}.json
            data (dict):
        """
        write_file(filepath_config(config_name), data)

    @timer
    def update_file(self, config_name):
        """
        Read, update and write config file.

        Args:
            config_name (str): ./config/{file}.json

        Returns:
            dict:
        """
        data = self.read_file(config_name)
        self.write_file(config_name, data)
        return data


if __name__ == '__main__':
    """
    Process the whole config generation.

                 task.yaml -+----------------> menu.json
             argument.yaml -+-> args.json ---> config_generated.py
             override.yaml -+       |
                  gui.yaml --------\|
                                   ||
    (old) i18n/<lang>.json --------\\========> i18n/<lang>.json
    (old)    template.json ---------\========> template.json
    """
    # Ensure running in Alas root folder
    import os
    os.chdir(os.path.join(os.path.dirname(__file__), '../../'))

    ConfigGenerator().generate()
    ConfigUpdater().update_file('template')
