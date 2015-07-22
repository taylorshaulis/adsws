import os
import requests
from flask.ext.headers import headers
from flask import request
from views import ProxyView
from adsws.modules.oauth2server.provider import oauth2
from urlparse import urljoin
import traceback
from importlib import import_module
from adsws.ext.ratelimiter import ratelimit, scope_func, limit_func
import netifaces
from dns.resolver import Resolver


class ConsulService:
    """
    Container for a consul service record
    """

    def __init__(self, service_uri):
        """
        :param service_uri: string formatted service identifier
            (consul://production.solr_service.consul)
        """
        assert service_uri.startswith('consul://'), "Invalid consul service URI"
        self.service_uri = service_uri
        self.service = service_uri.replace('consul://', '')
        self.endpoints = None
        self.resolver = Resolver()
        self.set_ns()

    def set_ns(self, iface='docker0', ip=None):
        """
        set the nameserver ip address from the network interface ip addr. If
        kwarg `ip` is specified, use that instead
        :param iface: network inferace
        :param ip: ip to return
        """
        if ip is not None:
            self.resolver.nameservers = ip
        else:
            assert iface in netifaces.interfaces(), \
                'Uknown iface {}'.format(iface)
            self.resolver.nameservers = [
                netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr']
            ]

    def resolve(self):
        """
        Query the consul DNS server for the service IP and port
        """
        endpoints = {}
        r = self.resolver.query(self.service, 'SRV')
        for rec in r.response.additional:
            name = rec.name.to_text()
            addr = rec.items[0].address
            endpoints[name] = {'addr': addr}
        for rec in r.response.answer[0].items:
            name = '.'.join(rec.target.labels)
            endpoints[name]['port'] = rec.port

        self.endpoints = [
            "http://{ip}:{port}".format(
                ip=v['addr'], port=v['port']
            ) for v in endpoints.values()
        ]
        return self.endpoints


def bootstrap_local_module(service_uri, deploy_path, app):
    """
    Incorporates the routes of an existing app into this one
    :param service_uri: the path to the target application
    :param deploy_path: the path on which to make the target app discoverable
    :param app: flask.Flask application instance
    :return: None
    """
    app.logger.debug(
        'Attempting bootstrap_local_module [{0}]'.format(service_uri)
    )

    module = import_module(service_uri)
    local_app = module.create_app()

    # Add the target app's config to the parent app's config.
    # Do not overwrite any config already present in the parent app
    for k, v in local_app.config.iteritems():
        if k not in app.config:
            app.config[k] = v

    for rule in local_app.url_map.iter_rules():
        view = local_app.view_functions[rule.endpoint]
        route = os.path.join(deploy_path, rule.rule[1:])

        # view_class is attached to a function view in the case of
        # class-based views, and that view.view_class is the element
        # that has the scopes and docstring attributes
        if hasattr(view, 'view_class'):
            attr_base = view.view_class
        else:
            attr_base = view

        # Decorate the view with ratelimit
        if hasattr(attr_base, 'rate_limit'):
            d = attr_base.rate_limit[0]
            view = ratelimit(
                limit=lambda default=d, **kwargs: limit_func(default),
                per=attr_base.rate_limit[1],
                scope_func=lambda: scope_func(),
                key_func=lambda: request.endpoint
            )(view)

        # Decorate the view with require_oauth
        if hasattr(attr_base, 'scopes'):
            view = oauth2.require_oauth(*attr_base.scopes)(view)

        # Add cache-control headers
        if app.config.get('API_PROXYVIEW_HEADERS'):
            view = headers(app.config['API_PROXYVIEW_HEADERS'])(view)

        # Let flask handle OPTIONS, which it will not do if we explicitly
        # add it to the url_map
        if 'OPTIONS' in rule.methods:
            rule.methods.remove('OPTIONS')
        app.add_url_rule(route, route, view, methods=rule.methods)


def bootstrap_remote_service(service_uri, deploy_path, app):
    """
    Incorporates the routes of a remote app into this one by registering
    views that forward traffic to those remote endpoints
    :param service_uri: the http url of the target application
    :param deploy_path: the path on which to make the target app discoverable
    :param app: flask.Flask application instance
    :return: None
    """

    app.logger.debug(
        'Attempting bootstrap_remote_service [{0}]'.format(service_uri)
    )
    url = urljoin(
        service_uri,
        app.config.get('WEBSERVICES_PUBLISH_ENDPOINT', '/')
    )

    try:
        r = requests.get(url, timeout=5)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        app.logger.info('Could not discover {0}'.format(service_uri))
        return

    # validate(r.json()) # TODO validate the incoming json

    # Start constructing the ProxyViews based on what we got when querying
    # the /resources route.
    # If any part of this procedure fails, log that we couldn't produce this
    # ProxyView, but otherwise continue.
    for resource, properties in r.json().iteritems():
        if resource.startswith('/'):
            resource = resource[1:]
        route = os.path.join(deploy_path, resource)
        remote_route = urljoin(service_uri, resource)

        # Make an instance of the ProxyView. We need to instantiate the class
        # to save instance attributes, which will be necessary to re-construct
        # the location to the third party resource (ProxyView.endpoint)
        proxyview = ProxyView(remote_route, service_uri, deploy_path)

        for method in properties['methods']:
            if method not in proxyview.methods:
                app.logger.warning("Could not create a ProxyView for "
                                   "method {meth} for {ep}"
                                   .format(meth=method, ep=service_uri))
                continue

            view = proxyview.dispatcher
            properties.setdefault('rate_limit', [1000, 86400])
            properties.setdefault('scopes', [])

            # Decorate the view with ratelimit.
            d = properties['rate_limit'][0]
            view = ratelimit(
                limit=lambda default=d, **kwargs: limit_func(default),
                per=properties['rate_limit'][1],
                scope_func=lambda: scope_func(),
                key_func=lambda: request.endpoint,
            )(view)

            # Decorate with the advertised oauth2 scopes
            view = oauth2.require_oauth(*properties['scopes'])(view)

            # Add cache-control headers
            if app.config.get('API_PROXYVIEW_HEADERS'):
                view = headers(app.config['API_PROXYVIEW_HEADERS'])(view)

            # Either make a new route with this view, or append the new method
            # to an existing route if one exists with the same name
            try:
                rule = next(app.url_map.iter_rules(endpoint=route))
                if method not in rule.methods:
                    rule.methods.update([method])
            except KeyError:
                app.add_url_rule(route, route, view, methods=[method])


def discover(app):
    """
    Query each third-party service defined in the config for a route that
    advertises that app's resources ('/resources' by default). Incorporate that
    app's routes into the api app, either directly (local module) or via
    proxying to a remote endpoint

    :param app: flask.Flask application instance
    :return: None
    """

    webservices = app.config.get('WEBSERVICES')
    if not webservices:
        webservices = {}
    for service_uri, deploy_path in webservices.iteritems():
        try:
            if any([
                    service_uri.startswith(prefix) for prefix in
                    ['http://', 'https://', 'consul://']
                    ]):
                bootstrap_remote_service(service_uri, deploy_path, app)
            else:
                bootstrap_local_module(service_uri, deploy_path, app)
        except:  # Continue bootstrapping, but log the traceback
            app.logger.error(
                "Problem discovering {service}, skipping this service "
                "entirely: {traceback}".format(
                    service=service_uri,
                    traceback=traceback.format_exc()
                )
            )
