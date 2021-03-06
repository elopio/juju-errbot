from base64 import b64decode
from contextlib import contextmanager
from functools import wraps
from glob import glob
from grp import getgrnam
from os import makedirs, path
from re import search
from shutil import move, rmtree
from subprocess import check_call, check_output
from tempfile import NamedTemporaryFile

from charmhelpers import fetch
from charmhelpers.core import hookenv
from charmhelpers.core.host import (
    adduser,
    add_group,
    chownr,
    lsb_release,
    restart_on_change,
    service_start,
    service_stop,
    user_exists,
    write_file,
)
from charmhelpers.core.templating import render
from charmhelpers.contrib.python.packages import pip_install

from charms.reactive import (
    remove_state,
    set_state,
    when,
    when_file_changed,
)


BASE_PATH = '/srv/errbot'
VAR_PATH = path.join(BASE_PATH, 'var')
LOG_PATH = path.join(VAR_PATH, 'log')
DATA_PATH = path.join(VAR_PATH, 'data')
PLUGIN_PATH = path.join(VAR_PATH, 'plugins')
ETC_PATH = path.join(BASE_PATH, 'etc')
VENV_PATH = path.join(BASE_PATH, 'venv')
WHEELS_PATH = path.join(BASE_PATH, 'wheels')
PIP_PATH = path.join(path.join(VENV_PATH, 'bin'), 'pip')
ERRBOT_PATH = path.join(VENV_PATH, path.join('bin', 'errbot'))
CONFIG_PATH = path.join(ETC_PATH, 'config.py')
PLUGINS_CONFIG_PATH = path.join(ETC_PATH, 'plugins_config.py')
SSH_HOME_PATH = path.join(path.join('/home', 'ubunet'), '.ssh')
UPSTART_PATH = '/etc/init/errbot.conf'
PATHS = (
    (VAR_PATH, 'ubunet', 'ubunet'),
    (LOG_PATH, 'errbot', 'errbot'),
    (DATA_PATH, 'errbot', 'errbot'),
    (PLUGIN_PATH, 'errbot', 'errbot'),
    (ETC_PATH, 'ubunet', 'ubunet'),
    (VENV_PATH, 'ubunet', 'ubunet'),
    (WHEELS_PATH, 'ubunet', 'ubunet'),
    (SSH_HOME_PATH, 'ubunet', 'ubunet'),
)
WEBHOOKS_PORT = 8080


@contextmanager
def ensure_user_and_perms(paths):
    def perms():
        for p in paths:
            makedirs(p[0], exist_ok=True)

            try:
                getgrnam(p[2])
            except KeyError:
                add_group(p[2], system_group=True)

            if not user_exists(p[1]):
                adduser(p[1], shell='/bin/false', system_user=True,
                        primary_group=p[2])

            # Ensure path is owned appropriately
            chownr(path=p[0], owner=p[1], group=p[2], chowntopdir=True)

    perms()
    yield
    perms()


def only_once_this_hook(f):
    """Decorator to ensure a function is called only once within a reactive
    hooks invocation, similar to the only_once decorator but it *will* be run
    again the next time another hook is invoked.
    """
    if not hasattr(f, '_only_once_this_hook__called'):
        f._only_once_this_hook__called = False

    @wraps(f)
    def wrapper(*args, **kwargs):
        if f._only_once_this_hook__called:
            return

        f._only_once_this_hook__called = True
        return f(*args, **kwargs)

    return wrapper


@only_once_this_hook
def setup_ssh_key():
    key = hookenv.config('private_ssh_key')
    if key:
        key = b64decode(key).decode('ascii')
        with ensure_user_and_perms(PATHS):
            key_type = 'rsa' if 'RSA' in key else 'dsa'
            key_path = path.join(SSH_HOME_PATH, 'id_{}'.format(key_type))
            write_file(key_path, key.encode('ascii'), owner='ubunet',
                       perms=0o500)

    elif path.exists(SSH_HOME_PATH):
        rmtree(SSH_HOME_PATH)


def get_wheels_store():
    """Returns the correct pip argument for a wheels dir or index URL, ensuring
    vcs stores are checked out appropriately.
    """

    repo = hookenv.config('wheels_repo')

    if not repo:
        return

    args = []
    repo_type = hookenv.config('wheels_repo_type').lower()

    if repo_type in ('git', 'bzr', 'hg', 'svn'):
        with NamedTemporaryFile() as f:
            render(source='errbot_peru.yaml.j2', target=f.name,
                   context={
                       'url': repo,
                       'module': ('curl' if repo_type == 'tar' else
                                  repo_type),
                       'revision': hookenv.config('wheels_repo_revision'),
                   })

            setup_ssh_key()
            with ensure_user_and_perms(PATHS):
                check_call(['sudo', 'su', '-s', '/bin/sh', '-', 'ubunet',
                            '-c',
                            ' '.join(('peru',
                                      '--file={}'.format(f.name),
                                      '--sync-dir={}'.format(WHEELS_PATH),
                                      'sync'))])

            args.append('--no-index')
            args.append('--find-links=file://{}'.format(WHEELS_PATH))
    elif repo_type in ('http', 'https', 'pypi'):
        args.append('--index-url={}'.format(repo))
    else:
        raise ValueError('Unknown wheels_repo_type: {}'.format(repo_type))

    return args


@when('config.changed.version')
def install_errbot():
    hookenv.status_set('maintenance', 'Installing packages')
    codename = lsb_release()['DISTRIB_CODENAME']
    if codename == 'trusty':
        venv_pkg = 'python3.4-venv'
    elif codename == 'xenial':
        venv_pkg = 'python3.5-venv'
    else:
        venv_pkg = 'python3-venv'
    apt_packages = [
        'python3',
        'python3-pip',
        'libssl-dev',
        'libffi-dev',
        'python3-dev',
        'git',
        venv_pkg,
    ]
    fetch.apt_install(fetch.filter_installed_packages(apt_packages))

    # Make sure we have a python3 virtualenv to install into
    with ensure_user_and_perms(PATHS):
        if not path.exists(PIP_PATH):
            hookenv.log('Creating python3 venv')
            check_call(['/usr/bin/python3', '-m', 'venv', VENV_PATH])
            pip_install('six', venv=VENV_PATH, upgrade=True)
            # Kill system six wheel copied into venv, as it's too old
            wheels_path = path.join(path.join(VENV_PATH, 'lib'),
                                    'python-wheels')
            hookenv.log('Removing six-1.5 wheel from venv')
            six_paths = glob(path.join(wheels_path, 'six-1.5*'))
            for p in six_paths:
                check_call(['rm', '-f', path.join(wheels_path, p)])

    version = hookenv.config('version')
    if not version:
        hookenv.log('version not set, skipping install of errbot',
                    level='WARNING')
        return

    current_version = ''
    if path.exists(ERRBOT_PATH):
        pip_show = check_output([PIP_PATH, 'show', 'errbot'])
        current_version_match = search('Version: (.*)',
                                       pip_show.decode('ascii'))
        if current_version_match.groups():
            current_version = current_version_match.groups()[0]

    if version != current_version:
        hookenv.status_set('maintenance',
                           'Installing configured version of errbot and'
                           ' dependencies')
        pip_pkgs = [
            'errbot=={}'.format(version),
        ]
        backend = hookenv.config('backend').lower()

        pip_pkg_map = {
            'irc': 'irc',
            'hipchat': 'hypchat',
            'slack': 'slackclient',
            'telegram': 'python-telegram-bot',
        }
        if backend in pip_pkg_map:
            pip_pkgs.append(pip_pkg_map[backend])

        if backend in ('xmpp', 'hipchat'):
            check_call(['/usr/bin/python3', '-m', 'venv',
                        '--system-site-packages', VENV_PATH])
            xmpp_pkgs = [
                'python3-dns',
                'python3-sleekxmpp',
                'python3-pyasn1',
                'python3-pyasn1-modules',
            ]
            fetch.apt_install(fetch.filter_installed_packages(xmpp_pkgs))

        pip_install(get_wheels_store() + pip_pkgs, venv=VENV_PATH)
    set_state('errbot.installed')


@when('errbot.installed')
@restart_on_change({
    CONFIG_PATH: ['errbot'],
    UPSTART_PATH: ['errbot'],
}, stopstart=True)
def render_config():
    hookenv.status_set('maintenance',
                       'Generating errbot configuration file')

    config_ctx = hookenv.config()
    config_ctx['data_path'] = DATA_PATH
    config_ctx['plugin_path'] = PLUGIN_PATH
    config_ctx['log_path'] = LOG_PATH

    upstart_ctx = {
        'venv_path': VENV_PATH,
        'user': 'errbot',
        'group': 'errbot',
        'working_dir': BASE_PATH,
        'config_path': CONFIG_PATH,
    }

    with ensure_user_and_perms(PATHS):
        render(source='errbot_config.py.j2',
               target=CONFIG_PATH,
               owner='errbot',
               perms=0o744,
               context=config_ctx)
        render(source='errbot_upstart.j2',
               target=UPSTART_PATH,
               owner='root',
               perms=0o744,
               context=upstart_ctx)

    set_state('errbot.available')


@when_file_changed(PLUGINS_CONFIG_PATH)
def configure_plugins():
    hookenv.status_set('maintenance', 'Installing/configuration plugins')
    # Shutdown errbot while we configure plugins, so we don't have concurrency
    # issues with the data files being updated
    service_stop('errbot')
    data_file = path.join(DATA_PATH, 'core.db')
    old_data_file = '{}.old'.format(data_file)
    if path.exists(data_file):
        move(data_file, old_data_file)
    try:
        env = {'ERRBOT_OLD_DATA_FILE': old_data_file}
        with ensure_user_and_perms(PATHS):
            check_output([ERRBOT_PATH, '--config', CONFIG_PATH,
                          '--restore', PLUGINS_CONFIG_PATH],
                         env=env)
    except Exception as e:
        hookenv.log('Error updating plugins: {}'.format(e),
                    level='ERROR')
        if path.exists(old_data_file):
            move(old_data_file, data_file)
    service_start('errbot')


@when('local-monitors.available', 'errbot.available')
def local_monitors(nagios):
    setup_nagios(nagios)


@when('nrpe-external-master.available', 'errbot.available')
def nrpe_external_master(nagios):
    setup_nagios(nagios)


def setup_nagios(nagios):
    hookenv.status_set('maintenance', 'Creating Nagios check')
    unit_name = hookenv.local_unit()
    nagios.add_check(['/usr/lib/nagios/plugins/check_procs',
                      '-c', '1:', '-a', 'bin/errbot'],
                     name="check_errbot_procs",
                     description="Verify at least one errbot process is "
                                 "running",
                     context=hookenv.config("nagios_context"),
                     unit=unit_name)


@when('errbot.available', 'config.changed.enable_webhooks')
def configure_webserver():
    render_plugin_config()

    if hookenv.config('enable_webhooks'):
        hookenv.open_port(WEBHOOKS_PORT)
        set_state('errbot.webhooks-enabled')
    else:
        hookenv.close_port(WEBHOOKS_PORT)
        remove_state('errbot.webhooks-enabled')


@when('errbot.available', 'config.changed.plugin_repos')
def configure_plugin_repos():
    render_plugin_config()


@when('errbot.available', 'config.changed.plugins_config')
def configure_plugins_config():
    render_plugin_config()


@only_once_this_hook
def render_plugin_config():
    with ensure_user_and_perms(PATHS):
        render(source='errbot_plugins_config.py.j2',
               target=PLUGINS_CONFIG_PATH,
               owner='errbot',
               perms=0o744,
               context=hookenv.config())


@when('webhooks.available', 'errbot.webhooks-enabled')
def configure_webhooks(webhooks):
        webhooks.configure(port=WEBHOOKS_PORT)
