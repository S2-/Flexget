import logging
import os
import posixpath
import time
from collections import namedtuple
from functools import partial
from itertools import groupby
from urllib.parse import quote, unquote, urljoin, urlparse

from loguru import logger

from flexget import plugin
from flexget.config_schema import one_or_more
from flexget.entry import Entry
from flexget.event import event
from flexget.utils.template import RenderError, render_from_entry

logger = logger.bind(name='sftp')

ConnectionConfig = namedtuple(
    'ConnectionConfig', ['host', 'port', 'username', 'password', 'private_key', 'private_key_pass']
)

# retry configuration constants
CONNECT_TRIES = 3
RETRY_INTERVAL = 15
RETRY_STEP = 5
SOCKET_TIMEOUT = 15

# make separate path instances for local vs remote path styles
localpath = os.path
remotepath = posixpath  # pysftp uses POSIX style paths

try:
    import pysftp

    logging.getLogger("paramiko").setLevel(logging.ERROR)
except ImportError:
    pysftp = None


def sftp_connect(conf):
    """
    Helper function to connect to an sftp server
    """
    sftp = None
    tries = CONNECT_TRIES
    retry_interval = RETRY_INTERVAL

    while not sftp:
        try:
            sftp = pysftp.Connection(
                host=conf.host,
                username=conf.username,
                private_key=conf.private_key,
                password=conf.password,
                port=conf.port,
                private_key_pass=conf.private_key_pass,
            )
            sftp.timeout = SOCKET_TIMEOUT
            logger.verbose('Connected to {}', conf.host)
        except Exception as e:
            if not tries:
                raise e
            else:
                logger.debug('Caught exception: {}', e)
                logger.warning(
                    'Failed to connect to {}; waiting {} seconds before retrying.',
                    conf.host,
                    retry_interval,
                )
                time.sleep(retry_interval)
                tries -= 1
                retry_interval += RETRY_STEP

    return sftp


def sftp_from_config(config):
    """
    Creates an SFTP connection from a Flexget config object
    """
    host = config['host']
    port = config['port']
    username = config['username']
    password = config['password']
    private_key = config['private_key']
    private_key_pass = config['private_key_pass']

    conn_conf = ConnectionConfig(host, port, username, password, private_key, private_key_pass)

    try:
        sftp = sftp_connect(conn_conf)
    except Exception as e:
        raise plugin.PluginError('Failed to connect to %s (%s)' % (host, e))

    return sftp


def sftp_prefix(config):
    """
    Generate SFTP URL prefix
    """
    login_str = ''
    port_str = ''

    if config['username'] and config['password']:
        login_str = '%s:%s@' % (config['username'], config['password'])
    elif config['username']:
        login_str = '%s@' % config['username']

    if config['port'] and config['port'] != 22:
        port_str = ':%d' % config['port']

    return 'sftp://%s%s%s/' % (login_str, config['host'], port_str)


def dependency_check():
    """
    Check if pysftp module is present
    """
    if not pysftp:
        raise plugin.DependencyError(
            issued_by='sftp',
            missing='pysftp',
            message='sftp plugin requires the pysftp Python module.',
        )


class SftpList:
    """
    Generate entries from SFTP. This plugin requires the pysftp Python module and its dependencies.

    Configuration:

    host:                 Host to connect to
    port:                 Port the remote SSH server is listening on. Defaults to port 22.
    username:             Username to log in as
    password:             The password to use. Optional if a private key is provided.
    private_key:          Path to the private key (if any) to log into the SSH server
    private_key_pass:     Password for the private key (if needed)
    recursive:            Indicates whether the listing should be recursive
    get_size:             Indicates whetern to calculate the size of the remote file/directory.
                          WARNING: This can be very slow when computing the size of directories!
    files_only:           Indicates wheter to omit diredtories from the results.
    dirs:                 List of directories to download

    Example:

      sftp_list:
          host: example.com
          username: Username
          private_key: /Users/username/.ssh/id_rsa
          recursive: False
          get_size: True
          files_only: False
          dirs:
              - '/path/to/list/'
              - '/another/path/'
    """

    schema = {
        'type': 'object',
        'properties': {
            'host': {'type': 'string'},
            'username': {'type': 'string'},
            'password': {'type': 'string'},
            'port': {'type': 'integer', 'default': 22},
            'files_only': {'type': 'boolean', 'default': True},
            'recursive': {'type': 'boolean', 'default': False},
            'get_size': {'type': 'boolean', 'default': True},
            'private_key': {'type': 'string'},
            'private_key_pass': {'type': 'string'},
            'dirs': one_or_more({'type': 'string'}),
        },
        'additionProperties': False,
        'required': ['host', 'username'],
    }

    def prepare_config(self, config):
        """
        Sets defaults for the provided configuration
        """
        config.setdefault('port', 22)
        config.setdefault('password', None)
        config.setdefault('private_key', None)
        config.setdefault('private_key_pass', None)
        config.setdefault('dirs', ['.'])

        return config

    def on_task_input(self, task, config):
        """
        Input task handler
        """

        dependency_check()

        config = self.prepare_config(config)

        files_only = config['files_only']
        recursive = config['recursive']
        get_size = config['get_size']
        private_key = config['private_key']
        private_key_pass = config['private_key_pass']
        dirs = config['dirs']
        if not isinstance(dirs, list):
            dirs = [dirs]

        logger.debug('Connecting to {}', config['host'])

        sftp = sftp_from_config(config)
        url_prefix = sftp_prefix(config)

        entries = []

        def file_size(path):
            """
            Helper function to get the size of a node
            """
            return sftp.lstat(path).st_size

        def dir_size(path):
            """
            Walk a directory to get its size
            """
            sizes = []

            def node_size(f):
                sizes.append(file_size(f))

            sftp.walktree(path, node_size, node_size, node_size, True)
            size = sum(sizes)

            return size

        def handle_node(path, size_handler, is_dir):
            """
            Generic helper function for handling a remote file system node
            """
            if is_dir and files_only:
                return

            url = urljoin(url_prefix, quote(sftp.normalize(path)))
            title = remotepath.basename(path)

            entry = Entry(title, url)

            if get_size:
                try:
                    size = size_handler(path)
                except Exception as e:
                    logger.error('Failed to get size for {} ({})', path, e)
                    size = -1
                entry['content_size'] = size

            if private_key:
                entry['private_key'] = private_key
                if private_key_pass:
                    entry['private_key_pass'] = private_key_pass

            entries.append(entry)

        # create helper functions to handle files and directories
        handle_file = partial(handle_node, size_handler=file_size, is_dir=False)
        handle_dir = partial(handle_node, size_handler=dir_size, is_dir=True)

        def handle_unknown(path):
            """
            Skip unknown files
            """
            logger.warning('Skipping unknown file: {}', path)

        # the business end
        for dir in dirs:
            try:
                sftp.walktree(dir, handle_file, handle_dir, handle_unknown, recursive)
            except IOError as e:
                logger.error('Failed to open {} ({})', dir, e)
                continue

        sftp.close()

        return entries


class SftpDownload:
    """
    Download files from a SFTP server. This plugin requires the pysftp Python module and its
    dependencies.

    Configuration:

    to:                 Destination path; supports Jinja2 templating on the input entry. Fields such
                        as series_name must be populated prior to input into this plugin using
                        metainfo_series or similar.
    recursive:          Indicates wether to download directory contents recursively.
    delete_origin:      Indicates wether to delete the remote files(s) once they've been downloaded.

    Example:

      sftp_download:
          to: '/Volumes/External/Drobo/downloads'
          delete_origin: False
    """

    schema = {
        'type': 'object',
        'properties': {
            'to': {'type': 'string', 'format': 'path'},
            'recursive': {'type': 'boolean', 'default': True},
            'delete_origin': {'type': 'boolean', 'default': False},
        },
        'required': ['to'],
        'additionalProperties': False,
    }

    def get_sftp_config(self, entry):
        """
        Parses a url and returns a hashable config, source path, and destination path
        """
        # parse url
        parsed = urlparse(entry['url'])
        host = parsed.hostname
        username = parsed.username or None
        password = parsed.password or None
        port = parsed.port or 22

        # get private key info if it exists
        private_key = entry.get('private_key')
        private_key_pass = entry.get('private_key_pass')

        if parsed.scheme == 'sftp':
            config = ConnectionConfig(
                host, port, username, password, private_key, private_key_pass
            )
        else:
            logger.warning('Scheme does not match SFTP: {}', entry['url'])
            config = None

        return config

    def download_file(self, path, dest, sftp, delete_origin):
        """
        Download a file from path to dest
        """
        dir_name = remotepath.dirname(path)
        dest_relpath = localpath.join(
            *remotepath.split(path)
        )  # convert remote path style to local style
        destination = localpath.join(dest, dest_relpath)
        dest_dir = localpath.dirname(destination)

        if localpath.exists(destination):
            logger.verbose('Destination file already exists. Skipping {}', path)
            return

        if not localpath.exists(dest_dir):
            os.makedirs(dest_dir)

        logger.verbose('Downloading file {} to {}', path, destination)

        try:
            sftp.get(path, destination)
        except Exception as e:
            logger.error('Failed to download {} ({})', path, e)
            if localpath.exists(destination):
                logger.debug('Removing partially downloaded file {}', destination)
                os.remove(destination)
            raise e

        if delete_origin:
            logger.debug('Deleting remote file {}', path)
            try:
                sftp.remove(path)
            except Exception as e:
                logger.error('Failed to delete file {} ({})', path, e)
                return

            self.remove_dir(sftp, dir_name)

    def handle_dir(self, path):
        """
        Dummy directory handler. Does nothing.
        """
        pass

    def handle_unknown(self, path):
        """
        Dummy unknown file handler. Warns about unknown files.
        """
        logger.warning('Skipping unknown file {}', path)

    def remove_dir(self, sftp, path):
        """
        Remove a directory if it's empty
        """
        if sftp.exists(path) and not sftp.listdir(path):
            logger.debug('Attempting to delete directory {}', path)
            try:
                sftp.rmdir(path)
            except Exception as e:
                logger.error('Failed to delete directory {} ({})', path, e)

    def download_entry(self, entry, config, sftp):
        """
        Downloads the file(s) described in entry
        """

        path = unquote(urlparse(entry['url']).path) or '.'
        delete_origin = config['delete_origin']
        recursive = config['recursive']

        to = config['to']
        if to:
            try:
                to = render_from_entry(to, entry)
            except RenderError as e:
                logger.error('Could not render path: {}', to)
                entry.fail(e)
                return

        if not sftp.lexists(path):
            logger.error('Remote path does not exist: {}', path)
            return

        if sftp.isfile(path):
            source_file = remotepath.basename(path)
            source_dir = remotepath.dirname(path)
            try:
                sftp.cwd(source_dir)
                self.download_file(source_file, to, sftp, delete_origin)
            except Exception as e:
                error = 'Failed to download file %s (%s)' % (path, e)
                logger.error(error)
                entry.fail(error)
        elif sftp.isdir(path):
            base_path = remotepath.normpath(remotepath.join(path, '..'))
            dir_name = remotepath.basename(path)
            handle_file = partial(
                self.download_file, dest=to, sftp=sftp, delete_origin=delete_origin
            )

            try:
                sftp.cwd(base_path)
                sftp.walktree(
                    dir_name, handle_file, self.handle_dir, self.handle_unknown, recursive
                )
            except Exception as e:
                error = 'Failed to download directory %s (%s)' % (path, e)
                logger.error(error)
                entry.fail(error)

                return

            if delete_origin:
                self.remove_dir(sftp, path)
        else:
            logger.warning('Skipping unknown file {}', path)

    def on_task_download(self, task, config):
        """
        Task handler for sftp_download plugin
        """
        dependency_check()

        # Download entries by host so we can reuse the connection
        for sftp_config, entries in groupby(task.accepted, self.get_sftp_config):
            if not sftp_config:
                continue

            error_message = None
            sftp = None
            try:
                sftp = sftp_connect(sftp_config)
            except Exception as e:
                error_message = 'Failed to connect to %s (%s)' % (sftp_config.host, e)
                logger.error(error_message)

            for entry in entries:
                if sftp:
                    self.download_entry(entry, config, sftp)
                else:
                    entry.fail(error_message)
            if sftp:
                sftp.close()


class SftpUpload:
    """
    Upload files to a SFTP server. This plugin requires the pysftp Python module and its
    dependencies.

    host:                 Host to connect to
    port:                 Port the remote SSH server is listening on. Defaults to port 22.
    username:             Username to log in as
    password:             The password to use. Optional if a private key is provided.
    private_key:          Path to the private key (if any) to log into the SSH server
    private_key_pass:     Password for the private key (if needed)
    to:                   Path to upload the file to; supports Jinja2 templating on the input entry. Fields such
                          as series_name must be populated prior to input into this plugin using
                          metainfo_series or similar.
    delete_origin:        Indicates wheter to delete the original file after a successful
                          upload.

    Example:

      sftp_list:
          host: example.com
          username: Username
          private_key: /Users/username/.ssh/id_rsa
          to: /TV/{{series_name}}/Series {{series_season}}
          delete_origin: False
    """

    schema = {
        'type': 'object',
        'properties': {
            'host': {'type': 'string'},
            'username': {'type': 'string'},
            'password': {'type': 'string'},
            'port': {'type': 'integer', 'default': 22},
            'private_key': {'type': 'string'},
            'private_key_pass': {'type': 'string'},
            'to': {'type': 'string'},
            'delete_origin': {'type': 'boolean', 'default': False},
        },
        'additionProperties': False,
        'required': ['host', 'username'],
    }

    def prepare_config(self, config):
        """
        Sets defaults for the provided configuration
        """
        config.setdefault('password', None)
        config.setdefault('private_key', None)
        config.setdefault('private_key_pass', None)
        config.setdefault('to', None)

        return config

    def handle_entry(self, entry, sftp, config, url_prefix):

        location = entry['location']
        filename = localpath.basename(location)

        to = config['to']
        if to:
            try:
                to = render_from_entry(to, entry)
            except RenderError as e:
                logger.error('Could not render path: {}', to)
                entry.fail(e)
                return

        destination = remotepath.join(to, filename)
        destination_url = urljoin(url_prefix, destination)

        if not os.path.exists(location):
            logger.warning('File no longer exists: {}', location)
            return

        if not sftp.lexists(to):
            try:
                sftp.makedirs(to)
            except Exception as e:
                logger.error('Failed to create remote directory {} ({})', to, e)
                entry.fail(e)
                return

        if not sftp.isdir(to):
            logger.error('Not a directory: {}', to)
            entry.fail('Not a directory: %s' % to)
            return

        try:
            sftp.put(localpath=location, remotepath=destination)
            logger.verbose('Successfully uploaded {} to {}', location, destination_url)
        except IOError as e:
            logger.error('Remote directory does not exist: {} ({})', to)
            entry.fail('Remote directory does not exist: %s (%s)' % to)
            return
        except Exception as e:
            logger.error('Failed to upload {} ({})', location, e)
            entry.fail('Failed to upload %s (%s)' % (location, e))
            return

        if config['delete_origin']:
            try:
                os.remove(location)
            except Exception as e:
                logger.error('Failed to delete file {} ({})', location, e)

    def on_task_output(self, task, config):
        """Uploads accepted entries to the specified SFTP server."""

        config = self.prepare_config(config)

        sftp = sftp_from_config(config)
        url_prefix = sftp_prefix(config)

        for entry in task.accepted:
            if sftp:
                logger.debug('Uploading file: {}', entry)
                self.handle_entry(entry, sftp, config, url_prefix)
            else:
                logger.debug('SFTP connection failed; failing entry: {}', entry)
                entry.fail('SFTP connection failed; failing entry: %s' % entry)


@event('plugin.register')
def register_plugin():
    plugin.register(SftpList, 'sftp_list', api_ver=2)
    plugin.register(SftpDownload, 'sftp_download', api_ver=2)
    plugin.register(SftpUpload, 'sftp_upload', api_ver=2)
