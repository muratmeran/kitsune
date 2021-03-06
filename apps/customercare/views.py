import calendar
from datetime import datetime
from email.utils import parsedate, formatdate
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import (HttpResponse, HttpResponseBadRequest,
                         HttpResponseNotFound, HttpResponseServerError)
from django.views.decorators.http import require_POST, require_GET
from django.utils.datastructures import SortedDict

from babel.numbers import format_number
import bleach
import jingo
from session_csrf import anonymous_csrf
from tower import ugettext as _, ugettext_lazy as _lazy
import tweepy

from customercare.models import CannedCategory, Tweet
import twitter


log = logging.getLogger('k.customercare')

MAX_TWEETS = 20
FILTERS = SortedDict([('recent', _lazy('Most Recent')),
                      ('unanswered', _lazy('Unanswered')),
                      ('answered', _lazy('Answered')),
                      ('all', _lazy('All'))])


def _tweet_for_template(tweet):
    """Return the dict needed for tweets.html to render a tweet + replies."""
    data = json.loads(tweet.raw_json)

    parsed_date = parsedate(data['created_at'])
    date = datetime(*parsed_date[0:6])

    # Recursively fetch replies.
    if settings.CC_SHOW_REPLIES:
        # If ever slow, optimize to do fewer queries.
        replies = _get_tweets(limit=0, reply_to=tweet)
    else:
        replies = None

    return {'profile_img': bleach.clean(data['profile_image_url']),
            'user': bleach.clean(data['from_user']),
            'text': bleach.clean(data['text']),
            'id': tweet.pk,
            'date': date,
            'reply_count': len(replies) if replies else 0,
            'replies': replies,
            'reply_to': tweet.reply_to and tweet.reply_to.pk,
            'hidden': tweet.hidden}


def _get_tweets(locale=settings.LANGUAGE_CODE, limit=MAX_TWEETS, max_id=None,
                reply_to=None, filter=None):
    """
    Fetch a list of tweets.

    Args:
        limit: the maximum number of tweets returned
        max_id: Return tweets with tweet_id less than this.
        reply_to: Return only tweets that are replies to the given Tweet. If
            None, return only top-level (non-reply) tweets.
        filter: One of the keys from FILTERS
    """
    locale = settings.LOCALES[locale].iso639_1
    # Uncached so we can immediately see our reply if we switch to the Answered
    # filter:
    q = Tweet.uncached.filter(locale=locale, reply_to=reply_to)
    if max_id:
        q = q.filter(pk__lt=max_id)

    if filter != 'all':
        q = q.filter(hidden=False)
    if filter == 'unanswered':
        q = q.filter(replies__pk__isnull=True)
    elif filter == 'answered':
        q = q.filter(replies__pk__isnull=False)

    if limit:
        q = q[:limit]

    return [_tweet_for_template(tweet) for tweet in q]


@require_GET
def more_tweets(request):
    """AJAX view returning a list of tweets."""
    max_id = request.GET.get('max_id')

    raw_filter = request.GET.get('filter')
    filter = raw_filter if raw_filter in FILTERS else 'recent'

    return jingo.render(request, 'customercare/tweets.html',
                        {'tweets': _get_tweets(locale=request.locale,
                                               max_id=max_id,
                                               filter=filter)})


@require_GET
@anonymous_csrf  # Need this so the anon csrf gets set for forms rendered.
@twitter.auth_wanted
def landing(request):
    """Customer Care Landing page."""

    twitter = request.twitter

    canned_responses = CannedCategory.objects.filter(locale=request.locale)

    # Stats. See customercare.cron.get_customercare_stats.
    activity = cache.get(settings.CC_TWEET_ACTIVITY_CACHE_KEY)
    if activity and 'resultset' in activity:
        activity_stats = []
        for act in activity['resultset']:
            if act is None:  # Sometimes we get bad data here.
                continue
            activity_stats.append((act[0], {
                'requests': format_number(act[1], locale='en_US'),
                'replies': format_number(act[2], locale='en_US'),
                'perc': act[3] * 100,
            }))
    else:
        activity_stats = []

    contributors = cache.get(settings.CC_TOP_CONTRIB_CACHE_KEY)
    if contributors and 'resultset' in contributors:
        contributor_stats = {}
        for contrib in contributors['resultset']:
            # Create one list per time period
            period = contrib[1]
            if not contributor_stats.get(period):
                contributor_stats[period] = []
            elif len(contributor_stats[period]) == 16:
                # Show a max. of 16 people.
                continue

            contributor_stats[period].append({
                'name': contrib[2],
                'username': contrib[3],
                'count': contrib[4],
                'avatar': contributors['avatars'].get(contrib[3]),
            })
    else:
        contributor_stats = {}

    return jingo.render(request, 'customercare/landing.html', {
        'activity_stats': activity_stats,
        'contributor_stats': contributor_stats,
        'canned_responses': canned_responses,
        'tweets': _get_tweets(locale=request.locale),
        'authed': twitter.authed,
        'twitter_user': (twitter.api.auth.get_username() if
                         twitter.authed else None),
        'filters': FILTERS,
    })


@require_POST
@anonymous_csrf
@twitter.auth_required
def twitter_post(request):
    """Post a tweet, and return a rendering of it (and any replies)."""

    try:
        reply_to_id = int(request.POST.get('reply_to', ''))
    except ValueError:
        # L10n: the tweet needs to be a reply to another tweet.
        return HttpResponseBadRequest(_('Reply-to is empty'))

    content = request.POST.get('content', '')
    if len(content) == 0:
        # L10n: the tweet has no content.
        return HttpResponseBadRequest(_('Message is empty'))

    if len(content) > 140:
        return HttpResponseBadRequest(_('Message is too long'))

    try:
        result = request.twitter.api.update_status(content, reply_to_id)
    except tweepy.TweepError, e:
        # L10n: {message} is an error coming from our twitter api library
        return HttpResponseBadRequest(
            _('An error occured: {message}').format(message=e))

    # Store reply in database.

    # If tweepy's status models actually implemented a dictionary, it would
    # be too boring.
    status = dict(result.__dict__)
    author = dict(result.author.__dict__)

    # Raw JSON blob data
    raw_tweet_data = {
        'id': status['id'],
        'text': status['text'],
        'created_at': formatdate(calendar.timegm(
            status['created_at'].timetuple())),
        'iso_language_code': author['lang'],
        'from_user_id': author['id'],
        'from_user': author['screen_name'],
        'profile_image_url': author['profile_image_url'],
    }

    # The tweet with id `reply_to_id` will not be missing from the DB unless
    # the purge cron job has run since the user loaded the form and we are
    # replying to a deleted tweet. TODO: Catch integrity error and log or
    # something.
    tweet = Tweet.objects.create(pk=status['id'],
                         raw_json=json.dumps(raw_tweet_data),
                         locale=author['lang'],
                         created=status['created_at'],
                         reply_to_id=reply_to_id)

    # We could optimize by not encoding and then decoding JSON.
    return jingo.render(request, 'customercare/tweets.html',
        {'tweets': [_tweet_for_template(tweet)]})


@require_POST
@anonymous_csrf
def hide_tweet(request):
    """
    Hide the tweet with a given ID. Only hides tweets that are not replies
    and do not have replies.

    Returns proper HTTP status codes.
    """
    # If feature disabled, bail.
    if not settings.CC_ALLOW_REMOVE:
        return HttpResponse(status=418)  # I'm a teapot.

    try:
        id = int(request.POST.get('id'))
    except (ValueError, TypeError):
        return HttpResponseBadRequest(_('Invalid ID.'))

    try:
        tweet = Tweet.objects.get(pk=id)
    except Tweet.DoesNotExist:
        return HttpResponseNotFound(_('Invalid ID.'))

    if (tweet.reply_to is not None or
        Tweet.objects.filter(reply_to=tweet).exists()):
        return HttpResponseBadRequest(_('Tweets that are replies or have '
                                        'replies must not be hidden.'))

    try:
        tweet.hidden = True
        tweet.save(force_update=True)
    except Exception, e:
        return HttpResponseServerError(
            _('An error occured: {message}').format(message=e))

    return HttpResponse('ok')
