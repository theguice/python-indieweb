#!flask/bin/python

"""
:copyright: (c) 2015 by Mike Taylor
:license: MIT, see LICENSE for more details.

A simple Flask web app to handle inbound
events that IndieWeb sites require.
"""

import os, sys
import json
import uuid
import urllib
import logging
import datetime

from urlparse import urlparse, ParseResult

import redis
import requests
import ronkyuu
import ninka

from bearlib.config import Config
from mf2py.parser import Parser
from flask import Flask, request, redirect, render_template, session, flash
from flask.ext.wtf import Form
from wtforms import TextField, HiddenField, BooleanField
from wtforms.validators import Required


class LoginForm(Form):
    me           = TextField('me', validators = [ Required() ])
    client_id    = HiddenField('client_id')
    redirect_uri = HiddenField('redirect_uri')
    from_uri     = HiddenField('from_uri')

class TokenForm(Form):
    code         = TextField('code', validators = [])
    me           = TextField('me', validators = [])
    redirect_uri = TextField('redirect_uri', validators = [])
    client_id    = TextField('client_id', validators = [])
    state        = TextField('state', validators = [])


# check for uwsgi, use PWD if present or getcwd() if not
_uwsgi = __name__.startswith('uwsgi')
if _uwsgi:
    _ourPath    = os.getenv('PWD', None)
    _configFile = '/etc/indieweb.cfg'
else:
    _ourPath    = os.getcwd()
    _configFile = os.path.join(_ourPath, 'indieweb.cfg')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'foo'  # replaced downstream
cfg = None
db  = None
templateData = {}
entries      = []

def baseDomain(domain, includeScheme=True):
    """Return only the network location portion of the given domain
    unless includeScheme is True
    """
    result = ''
    url    = urlparse(domain)
    if includeScheme:
        result = '%s://' % url.scheme
    if len(url.netloc) == 0:
        result += url.path
    else:
        result += url.netloc
    return result

def clearAuth():
    """Clear both the Session cookie and any stored token data
    """
    if 'indieauth_token' in session:
        indieauth_token = session['indieauth_token']
        if db is not None:
            key = db.get('token-%s' % indieauth_token)
            if key:
                data = db.hgetall(key)
                db.delete(key)
                db.delete('token-%s' % indieauth_token)
    session.pop('indieauth_token', None)
    session.pop('indieauth_scope', None)
    session.pop('indieauth_id', None)

def checkAuth():
    """Check if a valid Session cookie is found and the auth token within is valid
    """
    authed        = False
    indieauth_id  = None
    if 'indieauth_id' in session and 'indieauth_token' in session:
        indieauth_id    = session['indieauth_id']
        indieauth_token = session['indieauth_token']
        app.logger.info('session cookie found')
        if db is not None:
            key = db.get('token-%s' % indieauth_token)
            if key:
                data = db.hgetall(key)
                if data and data['token'] == indieauth_token:
                    authed = True
    return authed, indieauth_id

def checkAccessToken(access_token):
    """Check if the given access token matches any in the data stored
    """
    client_id  = None
    me         = None
    scope      = None
    key        = db.get('token-%s' % access_token)
    if key:
        data      = key.split('-')
        me        = data[1]
        client_id = data[2]
        scope     = data[3]

    return me, client_id, scope

@app.route('/logout', methods=['GET'])
def handleLogout():
    app.logger.info('handleLogout [%s]' % request.method)
    clearAuth()
    return redirect('/')

@app.route('/login', methods=['GET', 'POST'])
def handleLogin():
    app.logger.info('handleLogin [%s]' % request.method)

    form = LoginForm(me='', client_id=cfg['client_id'], 
                     redirect_uri='%s/success' % cfg['baseurl'], 
                     from_uri=request.args.get('from_uri'))

    if form.validate_on_submit():
        app.logger.info('me [%s]' % form.me.data)

        me            = baseDomain(form.me.data)
        authEndpoints = ninka.indieauth.discoverAuthEndpoints(me)

        if 'authorization_endpoint' in authEndpoints:
            authURL = None
            for url in authEndpoints['authorization_endpoint']:
                authURL = url
                break

            if authURL is not None:
                url = ParseResult(authURL.scheme, 
                                  authURL.netloc,
                                  authURL.path,
                                  authURL.params,
                                  urllib.urlencode({ 'me':            me,
                                                     'redirect_uri':  form.redirect_uri.data,
                                                     'client_id':     form.client_id.data,
                                                     'scope':         'post',
                                                     'response_type': 'id'
                                                   }),
                                  authURL.fragment).geturl()
                if db is not None:
                    key = 'login-%s' % me
                    data = db.hgetall(key)
                    if data: # clear any existing auth data
                        db.delete('token-%s' % data['token'])
                        db.hdel(key, 'token')
                    db.hset(key, 'from_uri',     form.from_uri.data)
                    db.hset(key, 'redirect_uri', form.redirect_uri.data)
                    db.hset(key, 'client_id',    form.client_id.data)
                    db.hset(key, 'scope',        'post')
                    db.expire(key, cfg['auth_timeout']) # expire in N minutes unless successful
                return redirect(url)
        else:
            return 'insert fancy no auth endpoint found error message here', 403

    templateData['title'] = 'Sign In'
    templateData['form']  = form
    return render_template('login.jinja', **templateData)

@app.route('/success', methods=['GET',])
def handleLoginSuccess():
    app.logger.info('handleLoginSuccess [%s]' % request.method)
    me   = request.args.get('me')
    code = request.args.get('code')
    app.logger.info('me [%s] code [%s]' % (me, code))

    if db is not None:
        app.logger.info('getting data to validate auth code')
        key  = 'login-%s' % me
        data = db.hgetall(key)
        if data:
            r = ninka.indieauth.validateAuthCode(code=code, 
                                                 client_id=me,
                                                 redirect_uri=data['redirect_uri'])
            if r['status'] == requests.codes.ok:
                app.logger.info('login code verified')
                scope    = r['response']['scope']
                from_uri = data['from_uri']
                token    = str(uuid.uuid4())

                db.hset(key, 'code',  code)
                db.hset(key, 'token', token)
                db.expire(key, cfg['auth_timeout'])
                db.set('token-%s' % token, key)
                db.expire('token-%s' % code, cfg['auth_timeout'])

                session['indieauth_token'] = token
                session['indieauth_scope'] = scope
                session['indieauth_id']    = me
            else:
                app.logger.info('login invalid')
                clearAuth()
        else:
            app.logger.info('nothing found for [%s]' % me)

    if scope:
        if from_uri:
            return redirect(from_uri)
        else:
            return redirect('/')
    else:
        return 'authentication failed', 403

@app.route('/auth', methods=['GET',])
def handleAuth():
    app.logger.info('handleAuth [%s]' % request.method)
    result = False
    if db is not None:
        token = request.args.get('token')
        if token is not None:
            me = db.get('token-%s' % token)
            if me:
                data = db.hgetall(me)
                if data and data['token'] == token:
                    result = True
    if result:
        return 'valid', 200
    else:
        session.pop('indieauth_token', None)
        session.pop('indieauth_scope', None)
        session.pop('indieauth_id', None)
        return 'invalid', 403


def handleMicropubEntry(data):
    # do something with the parameters sent by the micropub client
    # and return the new location and a 2## code
    return cfg.baseurl, 200

def processMicropub(data):
    if request.method == 'POST':
        if data['action'] not in ('entry',):
            return ('Micropub CREATE requires a valid action parameter', 400, [])
        else:
            location = None
            code     = 400
            action   = data['action'].lower()

            if action == 'entry':
                location, code = handleEntry(data)

            if code in (200,202,):
                return ('Micropub CREATE %s successful for %s' % (action, location), code, {'Location': location})
            else:
                return ('Micropub CREATE %s failed for %s' % (action, location), code, {})
    else:
        return ('Unable to process Micropub %s' % request.method, 400, [])

@app.route('/micropub', methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE'])
def handleMicroPub():
    app.logger.info('handleMicroPub [%s]' % request.method)

    access_token = request.headers.get('Authorization')
    if access_token:
        access_token = access_token.replace('Bearer ', '')
    me, client_id, scope = checkAccessToken(access_token)

    app.logger.info('micropub %s [%s] [%s, %s, %s]' % (request.method, access_token, me, client_id, scope))

    if me is None or client_id is None:
        return ('Invalid access_token', 400, {})
    else:
        if request.method == 'POST':
                domain = baseDomain(me, includeScheme=False)
                
                if domain == cfg.our_domain:
                    data = {}
                    for key in ('h', 'name', 'summary', 'content', 'published', 'updated', 'category', 
                                'slug', 'location', 'in-reply-to', 'repost-of', 'syndication', 'syndicate-to'):
                        data[key] = request.form.get(key)

                    return processMicropub(me, client_id, scope, data)
                else:
                    return 'unauthorized', 401
        elif request.method == 'GET':
            # add support for /micropub?q=syndicate-to
            return 'not implemented', 501

@app.route('/token', methods=['POST', 'GET'])
def handleToken():
    app.logger.info('handleToken [%s]' % request.method)

    if request.method == 'GET':
        access_token = request.headers.get('Authorization')
        if access_token:
            access_token = access_token.replace('Bearer ', '')
        me, client_id, scope = checkAccessToken(access_token)

        if me is None or client_id is None:
            return ('Token is not valid', 400, {})
        else:
            params = { 'me':        me,
                       'client_id': client_id,
                     }
            if scope is not None:
                params['scope'] = scope
            return (urllib.urlencode(params), 200, {'Content-Type': 'application/x-www-form-urlencoded'})

    elif request.method == 'POST':
        code         = request.form.get('code')
        me           = request.form.get('me')
        redirect_uri = request.form.get('redirect_uri')
        client_id    = request.form.get('client_id')
        state        = request.form.get('state')

        r = ninka.indieauth.validateAuthCode(code=code, 
                                             client_id=me,
                                             state=state,
                                             redirect_uri=redirect_uri)
        if r['status'] == requests.codes.ok:
            app.logger.info('token request auth code verified')
            scope = r['response']['scope']
            key   = 'app-%s-%s-%s' % (me, client_id, scope)
            token = db.get(key)
            if token is None:
                token     = str(uuid.uuid4())
                token_key = 'token-%s' % token
                db.set(key, token)
                db.set(token_key, key)

            app.logger.info('[%s] [%s]' % (key, token))

            params = { 'me': me,
                       'scope': scope,
                       'access_token': token
                     }
            return (urllib.urlencode(params), 200, {'Content-Type': 'application/x-www-form-urlencoded'})

def validURL(targetURL):
    """Validate the target URL exists.

    In a real app you would need to do a database lookup or a HEAD request, here we just check the URL
    """
    if '/article' in targetURL:
        result = 200
    else:
        result = 404
    return result

noteTemplate = """<span id="%(url)s"><p class="byline h-entry" role="note"> <a href="%(url)s">%(name)s</a> <time datetime="%(date)s">%(date)s</time></p></span>
%(marker)s
"""

def extractHCard(mf2Data):
    result = { 'name': '', 
               'url':  '',
             }
    if 'items' in mf2Data:
        for item in mf2Data['items']:
            if 'type' in item and 'h-card' in item['type']:
                result['name'] = item['properties']['name']
                if 'url' in item['properties']:
                    result['url'] = item['properties']['url']
    return result

def processVouch(sourceURL, targetURL, vouchDomain):
    """Determine if a vouch domain is valid.

    This implements a very simple method for determining if a vouch should
    be considered valid:
    1. does the vouch domain have it's own webmention endpoint
    2. does the vouch domain have an indieauth endpoint
    3. does the domain exist in the list of domains i've linked to

    yep, super simple but enough for me to test implement vouches
    """
    vouchFile = os.path.join(cfg['basepath'], 'vouch_domains.txt')
    with open(vouchFile, 'r') as h:
        vouchDomains = []
        for domain in h.readlines():
            vouchDomains.append(domain.strip().lower())

    if vouchDomain.lower() in vouchDomains:
        result = True
    else:
        wmStatus, wmUrl = ronkyuu.discoverEndpoint(vouchDomain, test_urls=False)
        if wmUrl is not None and wmStatus == 200:
            authEndpoints = ninka.indieauth.discoverAuthEndpoints(vouchDomain)

            if 'authorization_endpoint' in authEndpoints:
                authURL = None
                for url in authEndpoints['authorization_endpoint']:
                    authURL = url
                    break
                if authURL is not None:
                    result = True
                    with open(vouchFile, 'a+') as h:
                        h.write('\n%s' % vouchDomain)

def processWebmention(sourceURL, targetURL, vouchDomain=None):
    result = False
    r      = requests.get(sourceURL, verify=False)
    if r.status_code == requests.codes.ok:
        mentionData = { 'sourceURL':   sourceURL,
                        'targetURL':   targetURL,
                        'vouchDomain': vouchDomain,
                        'vouched':     False,
                        'received':    datetime.date.today().strftime('%d %b %Y %H:%M'),
                        'postDate':    datetime.date.today().strftime('%Y-%m-%dT%H:%M:%S')
                      }
        if 'charset' in r.headers.get('content-type', ''):
            mentionData['content'] = r.text
        else:
            mentionData['content'] = r.content

        if vouchDomain is not None and cfg['require_vouch']:
            mentionData['vouched'] = processVouch(sourceURL, targetURL, vouchDomain)
            result                 = mentionData['vouched']
            app.logger.info('result of vouch? %s' % result)
        else:
            result = not cfg['require_vouch']
            app.logger.info('no vouch domain, result %s' % result)

        mf2Data = Parser(doc=mentionData['content']).to_dict()
        hcard   = extractHCard(mf2Data)

        mentionData['hcardName'] = hcard['name']
        mentionData['hcardURL']  = hcard['url']
        mentionData['mf2data']   = mf2Data

        # Do something with the inbound mention

    return result

def mention(sourceURL, targetURL, vouchDomain=None):
    """Process the Webmention of the targetURL from the sourceURL.

    To verify that the sourceURL has indeed referenced our targetURL
    we run findMentions() at it and scan the resulting href list.
    """
    app.logger.info('discovering Webmention endpoint for %s' % sourceURL)

    mentions = ronkyuu.findMentions(sourceURL)
    result   = False
    app.logger.info('mentions %s' % mentions)
    for href in mentions['refs']:
        if href != sourceURL and href == targetURL:
            app.logger.info('post at %s was referenced by %s' % (targetURL, sourceURL))

            result = processWebmention(sourceURL, targetURL, vouchDomain)
    app.logger.info('mention() returning %s' % result)
    return result

@app.route('/webmention', methods=['POST'])
def handleWebmention():
    app.logger.info('handleWebmention [%s]' % request.method)
    if request.method == 'POST':
        valid  = False
        source = request.form.get('source')
        target = request.form.get('target')
        vouch  = request.form.get('vouch')
        app.logger.info('source: %s target: %s vouch %s' % (source, target, vouch))

        valid = validURL(target)

        app.logger.info('valid? %s' % valid)

        if valid == requests.codes.ok:
            if mention(source, target, vouch):
                return redirect(target)
            else:
                if vouch is None and cfg['require_vouch']:
                    return 'Vouch required for webmention', 449
                else:
                    return 'Webmention is invalid', 400
        else:
            return 'invalid post', 404

@app.route('/article<article>', methods=['GET'])
def handleArticles(article):
    app.logger.info('handleArticles %s article%s' % (request.method, article))

    templateData['entries'] = entries
    return render_template('index.jinja', **templateData)

@app.route('/', methods=['GET'])
def handleRoot():
    app.logger.info('handleRoot [%s]' % request.method)

    templateData['entries'] = entries
    return render_template('index.jinja', **templateData)

def initLogging(logger, logpath=None, echo=False):
    logFormatter = logging.Formatter("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S")

    if logpath is not None:
        from logging.handlers import RotatingFileHandler

        logfilename = os.path.join(logpath, 'indieweb.log')
        logHandler  = logging.handlers.RotatingFileHandler(logfilename, maxBytes=1024 * 1024 * 100, backupCount=7)
        logHandler.setFormatter(logFormatter)
        logger.addHandler(logHandler)

    if echo:
        echoHandler = logging.StreamHandler()
        echoHandler.setFormatter(logFormatter)
        logger.addHandler(echoHandler)

    logger.setLevel(logging.INFO)
    logger.info('starting Indieweb App')

def loadConfig(configFilename, host=None, port=None, basepath=None, logpath=None):
    result = Config()
    result.fromJson(configFilename)

    if host is not None and 'host' not in result:
        result.host = host
    if port is not None and 'port' not in result:
        result.port = port
    if basepath is not None and 'basepath' not in result:
        result.basepath = basepath
    if logpath is not None and 'logpath' not in result:
        result.logpath = logpath
    if 'auth_timeout' not in result:
        result.auth_timeout = 300
    if 'require_vouch' not in result:
        result.require_vouch = False

    return result

def getRedis(cfgRedis):
    if 'host' not in cfgRedis:
        cfgRedis.host = '127.0.0.1'
    if 'port' not in cfgRedis:
        cfgRedis.port = 6379
    if 'db' not in cfgRedis:
        cfgRedis.db = 0

    return redis.StrictRedis(host=cfgRedis.host, port=cfgRedis.port, db=cfgRedis.db)

def buildTemplateContext(config):
    result = {}
    for key in ('baseurl', 'title', 'meta'):
        if key in config.bearlog:
            value = config.bearlog[key]
        else:
            value = ''
        result[key] = value
    return result

def doStart(app, configFile, ourHost=None, ourPort=None, ourBasePath=None, ourPath=None, echo=False):
    _cfg = loadConfig(configFile, host=ourHost, port=ourPort, basepath=ourBasePath, logpath=ourPath)
    _db  = None
    if 'secret' in _cfg:
        app.config['SECRET_KEY'] = _cfg.secret
    initLogging(app.logger, _cfg.logpath, echo=echo)
    if 'redis' in _cfg:
        _db = getRedis(_cfg.redis)
    return _cfg, _db

if _uwsgi:
    cfg, db = doStart(app, _configFile, _ourPath)
    templateData = buildTemplateContext(cfg)
#
# None of the below will be run for nginx + uwsgi
#
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--host',     default='0.0.0.0')
    parser.add_argument('--port',     default=5000, type=int)
    parser.add_argument('--logpath',  default='/var/log')
    parser.add_argument('--basepath', default='/var/www')
    parser.add_argument('--config',   default='/etc/indieweb.cfg')

    args = parser.parse_args()

    cfg, db = doStart(app, args.config, args.host, args.port, args.basepath, args.logpath, echo=True)
    templateData = buildTemplateContext(cfg)

    for i in range(1, 3):
        entries.append({ 'title': 'Article %d' % i,
                         'slug':  'article%d' % i,
                         'date':  datetime.datetime(2015,1,i, 10, 0, 0),
                         'text':  'test article %d' % i
                       })

    app.run(host=cfg.host, port=cfg.port, debug=True)
