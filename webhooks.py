# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 Carlos Jenkins <carlos@jenkins.co.cr>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
from sys import stderr
logging.basicConfig(stream=stderr)

import hmac
from hashlib import sha1
from json import loads, dumps
from subprocess import Popen, PIPE
from tempfile import mkstemp
from os import access, X_OK, remove
from os.path import isfile, abspath, normpath, dirname, join, basename

import requests
from ipaddress import ip_address, ip_network
from flask import Flask, request, abort

from ansible import playbook, inventory, callbacks, utils
from helpers.callbacks import CustomAggregateStats

# Boilerplace callbacks for stdout/stderr and log output
utils.VERBOSITY = 0
playbook_cb = callbacks.PlaybookCallbacks(verbose=utils.VERBOSITY)
stats = CustomAggregateStats()
runner_cb = callbacks.PlaybookRunnerCallbacks(stats, verbose=utils.VERBOSITY)

application = Flask(__name__)

@application.route('/ansible', methods=['GET', 'POST'])
def fleet():
    """
    Execute fleet commands on remote infrastructure
        #playbook.PlayBook(
            playbook  = book,
            inventory = INVENTORY,
            transport = 'local',
            callbacks = callbacks.PlaybookCallbacks(),
            runner_callbacks = callbacks.DefaultRunnerCallbacks(),
            stats  = callbacks.AggregateStats(),
        ).run()
    """
    request_data = request.form.to_dict() #loads(request.data)

    def retrieve_vars(text, var_assign=':', var_split=','):
        result = {}
        for var in text.split(var_split):
            var_parts = var.split(var_assign)
            result[var_parts[0].strip()] = var_parts[1].strip()
        return result
    payload = retrieve_vars(request_data['text'])
    pb = playbook.PlayBook(
            playbook='/src/playbooks/{0}.yml'.format(payload['playbook']),
            inventory=inventory.Inventory("/etc/ansible/ec2.py"),
            extra_vars=retrieve_vars(payload['extra_vars'], '=', ' '),
            #remote_user='core',
            #private_key_file='./automation/keys/id_rsa',
            transport = 'smart',
            callbacks = playbook_cb,
            runner_callbacks = runner_cb,
            stats  = stats,
    )
    return dumps(pb.run(), sort_keys=True, indent=4)

@application.route('/github', methods=['GET', 'POST'])
def index():
    """
    Main WSGI application entry.
    """

    path = normpath(abspath(dirname(__file__)))
    hooks = join(path, 'hooks')

    # Only POST is implemented
    if request.method != 'POST':
        abort(501)

    # Load config
    with open(join(path, 'config.json'), 'r') as cfg:
        config = loads(cfg.read())

    # Allow Github IPs only
    if config.get('github_ips_only', True):
        src_ip = ip_address(
            u'{}'.format(request.access_route[0])  # Fix stupid ipaddress issue
        )
        whitelist = requests.get('https://api.github.com/meta').json()['hooks']

        for valid_ip in whitelist:
            if src_ip in ip_network(valid_ip):
                break
        else:
            pass#abort(403)

    # Enforce secret
    secret = config.get('enforce_secret', '')
    if secret:
        # Only SHA1 is supported
        sha_name, signature = request.headers.get('X-Hub-Signature').split('=')
        if sha_name != 'sha1':
            abort(501)

        # HMAC requires the key to be bytes, but data is string
        mac = hmac.new(str(secret), msg=request.data, digestmod=sha1)
        if not hmac.compare_digest(str(mac.hexdigest()), str(signature)):
            abort(403)

    # Implement ping
    event = request.headers.get('X-GitHub-Event', 'ping')
    if event == 'ping':
        return dumps({'msg': 'pong'})

    # Gather data
    try:
        payload = loads(request.data)
        meta = {
            'name': payload['repository']['name'],
            'branch': payload['ref'].split('/')[2],
            'event': event
        }
    except:
        abort(400)

    # Possible hooks
    scripts = [
        join(hooks, '{event}-{name}-{branch}'.format(**meta)),
        join(hooks, '{event}-{name}'.format(**meta)),
        join(hooks, '{event}'.format(**meta)),
        join(hooks, 'all')
    ]

    # Check permissions
    scripts = [s for s in scripts if isfile(s) and access(s, X_OK)]
    if not scripts:
        return dumps({'msg': meta})

    # Save payload to temporal file
    _, tmpfile = mkstemp()
    with open(tmpfile, 'w') as pf:
        pf.write(dumps(payload))

    # Run scripts
    ran = {}
    for s in scripts:

        proc = Popen(
            [s, tmpfile, event],
            stdout=PIPE, stderr=PIPE
        )
        stdout, stderr = proc.communicate()

        ran[basename(s)] = {
            'returncode': proc.returncode,
            'stdout': stdout,
            'stderr': stderr,
        }

        # Log errors if a hook failed
        if proc.returncode != 0:
            logging.error('{} : {} \n{}'.format(
                s, proc.returncode, stderr
            ))

    # Remove temporal file
    remove(tmpfile)

    info = config.get('return_scripts_info', False)
    if not info:
        return ''

    output = dumps(ran, sort_keys=True, indent=4)
    logging.info(output)
    return output


if __name__ == '__main__':
    application.run(debug=True, host='0.0.0.0')
