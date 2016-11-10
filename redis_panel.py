from itertools import chain
import operator
import os
from redis import Redis as OriginalRedis, StrictRedis as OriginalStrictRedis
import redis
import time

from debug_toolbar.panels import Panel
from debug_toolbar.utils import get_stack, tidy_stacktrace
from django.conf import settings
from django.dispatch import Signal
from django.template.base import Template
from django.template.context import Context
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _, ungettext
from redis.client import BasePipeline, Pipeline as OriginalPipeline, StrictPipeline as OriginalStrictPipeline


__all__ = ['redis_call', 'TrackingRedisMixin', 'TrackingRedis',
           'StrictTrackingRedis', 'BaseTrackingPipeline', 'RedisPanel',
           'TrackingPipelineMixin', 'TrackingPipeline',
           'StrictTrackingPipeline']


redis_call = Signal(providing_args=['duration', 'calls'])


class TrackingRedisBase(object):
    def make_call_dict(self, depth, *args, **kwargs):
        debug_config = getattr(settings, 'DEBUG_TOOLBAR_CONFIG', {})
        enable_stack = debug_config.get('ENABLE_STACKTRACES', True)

        trace = enable_stack and tidy_stacktrace(reversed(get_stack()))[:-depth - 1] or []

        # prepare arguments for display
        arguments = map(repr, args[2:])
        options = map(lambda k, v: "%s=%s" % (k, repr(v)), kwargs.items())

        return {'function': args[0],
                'key': len(args) > 1 and args[1] or '',
                'args': ' , '.join(chain(arguments, options)),
                'trace': trace}


class TrackingRedisMixin(TrackingRedisBase):
    def execute_command(self, func_name, *args, **kwargs):
        call = self.make_call_dict(2, func_name, *args, **kwargs)

        try:
            start = time.time()
            ret = super(TrackingRedisMixin, self).execute_command(func_name,
                                                                  *args, **kwargs)
            call['return'] = str(ret)
        finally:
            stop = time.time()
            duration = (stop - start) * 1000

            redis_call.send_robust(sender=self, duration=duration, calls=(call,))

        return ret


class BaseTrackingPipeline(TrackingRedisBase, BasePipeline):
    def execute(self, *args, **kw):
        tr = {'calls': []}

        for arguments, options in self.command_stack:
            tr['calls'].append(self.make_call_dict(1, *arguments, **options))

        try:
            start = time.time()
            ret = super(BaseTrackingPipeline, self).execute(*args, **kw)

            for i, call in enumerate(tr['calls']):
                call['return'] = str(ret[i])
        finally:
            stop = time.time()
            tr['duration'] = (stop - start) * 1000

            redis_call.send_robust(sender=self, **tr)

        return ret


class TrackingRedis(TrackingRedisMixin, OriginalRedis):
    def pipeline(self, transaction=False, shard_hint=None):
        return TrackingPipeline(self.connection_pool,
                                self.response_callbacks,
                                transaction,
                                shard_hint)


class StrictTrackingRedis(TrackingRedisMixin, OriginalStrictRedis):
    def pipeline(self, transaction=False, shard_hint=None):
        return StrictTrackingPipeline(self.connection_pool,
                                      self.response_callbacks,
                                      transaction,
                                      shard_hint,)


class TrackingPipeline(BaseTrackingPipeline, OriginalRedis):
    pass


class StrictTrackingPipeline(BaseTrackingPipeline, OriginalStrictRedis):
    pass


class RedisPanel(Panel):
    name = 'Redis'
    has_content = True

    def __init__(self, *args, **kwargs):
        super(RedisPanel, self).__init__(*args, **kwargs)
        self.calls = []
        redis_call.connect(self._add_call)

    def _add_call(self, sender, duration, calls, **kw):
        for call in calls:
            call['trace'] = render_stacktrace(call['trace'])
        self.calls.append({'duration': duration, 'calls': calls})

    def nav_title(self):
        return _("Redis")
    title = nav_title

    def nav_subtitle(self):
        calls = len(self.calls)
        duration = sum(map(operator.itemgetter('duration'), self.calls))

        return ungettext('%(calls)d call in %(duration).2fms',
                         '%(calls)d calls in %(duration).2fms',
                         calls) % {'calls': calls, 'duration': duration}

    def url(self):
        return ''

    @property
    def content(self):
        debug_config = getattr(settings, 'DEBUG_TOOLBAR_CONFIG', {})
        enable_stack = debug_config.get('ENABLE_STACKTRACES', True)
        context = {'calls': self.calls,
                   'commands': {},
                   'enable_stack': enable_stack}
        for tr in self.calls:
            for call in tr['calls']:
                context['commands'][call['function']] = context['commands'].get(call['function'], 0) + 1
        return Template(template).render(Context(context))

    def enable_instrumentation(self):
        redis.Redis = TrackingRedis
        redis.StrictRedis = StrictTrackingRedis
        redis.client.Pipeline = TrackingPipeline
        redis.client.StrictPipeline = StrictTrackingPipeline

    def disable_instrumentation(self):
        redis.Redis = OriginalRedis
        redis.StrictRedis = OriginalStrictRedis
        redis.client.Pipeline = OriginalPipeline
        redis.client.StrictPipeline = OriginalStrictPipeline


def render_stacktrace(trace):
    stacktrace = []
    for frame in trace:
        params = map(escape, frame[0].rsplit(os.path.sep, 1) + list(frame[1:]))
        try:
            stacktrace.append(u'<span class="path">{0}/</span><span class="file">{1}</span> in <span class="func">{3}</span>(<span class="lineno">{2}</span>)\n  <span class="code">{4}</span>'.format(*params))
        except IndexError:
            # This frame doesn't have the expected format, so skip it and move on to the next one
            continue
    return mark_safe('\n'.join(stacktrace))


template = """
{% load i18n %}
<h4>{% trans "Calls" %}</h4>
<table>
    <thead>
        <tr>
            <th>{% trans "Command" %}</th>
            <th>{% trans "Count" %}</th>
        </tr>
    </thead>
    <tbody>
    {% for command, count in commands.iteritems %}
        <tr>
            <td>{{ command }}</td>
            <td>{{ count }}</td>
        </tr>
    {% endfor %}
    </tbody>
</table>

<table>
    <thead>
        <tr>
            <th>{% trans "Duration" %}</th>
            <th>{% trans "Call" %}</th>
            <th>{% trans "Key" %}</th>
            <th>{% trans "Args" %}</th>
            <th>{% trans "Result" %}</th>
            <th>{% trans "Action" %}</th>
        </tr>
    </thead>

    <tbody>
        {% for tr in calls %}
        {% for call in tr.calls %}
        <tr>
            <td>{% if forloop.first %}{{ tr.duration }} ms{% endif %}</td>
            <td>{{ call.function }}</td>
            <td>{{ call.key }}</td>
            <td>{{ call.args }}</td>
            <td>{{ call.return }}</td>
            <td>{% if enable_stack %}<a href="#" class="djdtRedisShowTrace">{% trans "Show stacktrace" %}</a>{% endif %}</td>
        </tr>

        {% if call.trace %}
            <tr class="djdtRedisTrace" style="display:none">
                <td colspan="6">
                    <pre class="stack">{{ call.trace }}</pre>
                </td>
            </tr>
        {% endif %}

        {% endfor %}
        {% endfor %}
    </tbody>
</table>
<script type="text/javascript">
// the way ddt is using jquery we can't get at it after the fact in the usual
// manner
{% if enable_stack %}
    (function(window, document) {
        var j = window.djdt.jQuery;
        j(document).ready(function() {
            j('.djdtRedisShowTrace').click(function () {
                j(this).parent().parent().next().toggle()
            })
    });
    })(window, document);
{% endif %}
</script>
"""
