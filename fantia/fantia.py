#!/usr/bin/env python3

import os
import re
import json
from datetime import datetime, timezone
import dateutil.parser
from tempfile import mkstemp
import shutil
from urllib.parse import urlparse
import functools
import requests

import hoordu
from hoordu.models import *
from hoordu.plugins import *
from hoordu.forms import *

POST_FORMAT = 'https://fantia.jp/posts/{post_id}'
POST_REGEXP = re.compile('^https?:\/\/fantia\.jp\/posts\/(?P<post_id>\d+)(?:\?.*)?(?:#.*)?$')
FANCLUB_REGEXP = re.compile('^https?:\/\/fantia\.jp\/fanclubs\/(?P<fanclub_id>\d+)(?:\/.*)?(?:\?.*)?(?:#.*)?$')
FILENAME_REGEXP = re.compile('^[a-z0-9]+-(?P<filename>.+)$')

POST_GET_URL = 'https://fantia.jp/api/v1/posts/{post_id}'
FANCLUB_GET_URL = 'https://fantia.jp/api/v1/fanclubs/{fanclub_id}'
FILE_DOWNLOAD_URL = 'https://fantia.jp{download_uri}'

class CreatorIterator:
    def __init__(self, fantia, subscription=None, options=None):
        self.fantia = fantia
        self.http = fantia.http
        self.log = fantia.log
        self.subscription = subscription
        
        if self.subscription is not None:
            options = hoordu.Dynamic.from_json(self.subscription.options)
            self.state = hoordu.Dynamic.from_json(self.subscription.state)
        else:
            self.state = hoordu.Dynamic()
        
        self.creator_id = options.creator_id
        
        self.head_id = self.state.get('head_id')
        self.tail_id = self.state.get('tail_id')
    
    def _save_state(self):
        self.state.head_id = self.head_id
        self.state.tail_id = self.tail_id
        if self.subscription is not None:
            self.subscription.state = self.state.to_json()
    
    def _post_iterator(self, direction=FetchDirection.newer, n=None):
        post_id = self.head_id if direction == FetchDirection.newer else self.tail_id
        
        if post_id is None:
            response = self.http.get(FANCLUB_GET_URL.format(fanclub_id=self.creator_id))
            response.raise_for_status()
            fanclub = hoordu.Dynamic.from_json(response.text).fanclub
            
            if not fanclub.recent_posts:
                return
            
            post_id = fanclub.recent_posts[0].id
            self.head_id = post_id
            self.tail_id = post_id
            
        else:
            # TODO the post might have been deleted
            # there's no issue if we get all the posts from the beginning up until the head
            # but there's no way to start at the end without going through everything again
            response = self.http.get(POST_GET_URL.format(post_id=post_id))
            response.raise_for_status()
            post = hoordu.Dynamic.from_json(response.text).post
            
            next_post = post.links.next if direction == FetchDirection.newer else post.links.previous
            if next_post is None:
                return
            
            post_id = next_post.id
        
        it = range(n) if n is not None else iter(int, 1)
        for _ in it:
            response = self.http.get(POST_GET_URL.format(post_id=post_id))
            response.raise_for_status()
            post = hoordu.Dynamic.from_json(response.text).post
            self.log.debug('post: %s', post)
            
            yield post
            
            if direction == FetchDirection.newer:
                self.head_id = post_id
            elif direction == FetchDirection.older:
                self.tail_id = post_id
            
            next_post = post.links.next if direction == FetchDirection.newer else post.links.previous
            if next_post is None:
                break
            
            post_id = next_post.id
    
    def fetch(self, direction=FetchDirection.newer, n=None):
        """
        Try to get at least `n` newer or older posts from this search
        depending on the direction.
        Create a RemotePost entry and any associated Files for each post found,
        thumbnails should be downloaded, files are optional.
        Posts should always come ordered in the same way.
        
        Returns a list of the new RemotePost objects.
        """
        
        if self.tail_id is None:
            direction = FetchDirection.older
        
        for post in self._post_iterator(direction, n):
            remote_posts = self.fantia._to_remote_posts(post, preview=self.subscription is None)
            for remote_post in remote_posts:
                yield remote_post
            
            if self.subscription is not None:
                for p in remote_posts:
                    self.subscription.feed.append(p)
            
            # always commit changes
            # RemotePost, RemoteTag and the subscription feed are simply a cache
            # the file downloads are more expensive than a call to the database
            self.fantia.core.commit()
        
        self._save_state()
        if self.subscription is not None:
            self.fantia.core.add(self.subscription)

class Fantia:
    name = 'fantia'
    version = 1
    
    @classmethod
    def config_form(cls):
        return Form('{} config'.format(cls.name),
            ('session_id', Input('_session_id cookie', [validators.required]))
        )
    
    @classmethod
    def init(cls, core, parameters=None):
        source = core.source
        
        cls.update(core)
        
        # check if everything is ready to use
        config = hoordu.Dynamic.from_json(source.config)
        
        if not config.defined('session_id'):
            # try to get the values from the parameters
            if parameters is not None:
                config.update(parameters)
                
                source.config = json.dumps(config)
                core.add(source)
        
        if not config.defined('session_id'):
            # but if they're still None, the api can't be used
            return False, cls.config_form()
            
        else:
            # the config contains every required property
            return True, cls(core)
    
    @classmethod
    def update(cls, core):
        source = core.source
        
        if source.version < cls.version:
            # update anything if needed
            
            # if anything was updated, then the db entry should be updated as well
            source.version = cls.version
            core.add(source)
    
    def __init__(self, core, config=None):
        self.core = core
        self.source = core.source
        self.log = core.logger
        self.session = core.session
        
        if config is None:
            config = hoordu.Dynamic.from_json(self.source.config)
        
        self._load_config(config)
        
        self._init_api()
    
    def _load_config(self, config):
        self.session_id = config.session_id
    
    def _init_api(self):
        self.http = requests.Session()
        
        self.http.headers.update({
            'Origin': 'https://fantia.jp/',
            'Referer': 'https://fantia.jp/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/82.0'
        })
        
        cookie = requests.cookies.create_cookie(name='_session_id', value=self.session_id)
        self.http.cookies.set_cookie(cookie)
    
    def parse_url(self, url):
        """
        Checks if an url can be downloaded by this plugin.
        
        Returns the remote id if the url corresponds to a single post,
        a Dynamic object that can be passed to search if the url
        corresponds to multiple posts, or None if this plugin can't
        download or create a search with this url.
        """
        
        if url.isdigit():
            return url
        
        match = POST_REGEXP.match(url)
        if match:
            return match.group('post_id')
        
        match = FANCLUB_REGEXP.match(url)
        if match:
            return hoordu.Dynamic({
                'creator_id': match.group('fanclub_id')
            })
        
        return post_id
    
    def _download_file(self, url, filename=None):
        # TODO file downloads should be managed by hoordu
        # so that rate limiting and a download manager can be
        # implemented easily and in a centralized way
        self.log.debug('downloading %s', url)
        
        if filename is not None:
            suffix = '-{}'.format(filename)
            
        else:
            suffix = os.path.splitext(urlparse(url).path)[-1]
            if not suffix.startswith('.'):
                suffix = ''
        
        fd, path = mkstemp(suffix=suffix)
        
        with self.http.get(url, stream=True) as resp:
            resp.raise_for_status()
            resp.raw.read = functools.partial(resp.raw.read, decode_content=True)
            with os.fdopen(fd, 'w+b') as file:
                shutil.copyfileobj(resp.raw, file)
        
        return path
    
    def _content_to_post(self, post, content, remote_post=None, preview=False):
        content_id = '{post_id}-{content_id}'.format(post_id=post.id, content_id=content.id)
        creator_id = str(post.fanclub.id)
        creator_name = post.fanclub.user.name
        # possible timezone issues?
        post_time = dateutil.parser.parse(post.posted_at).astimezone(timezone.utc)
        
        self.log.info('getting post %s', content_id)
        
        if remote_post is None:
            remote_post = self.session.query(RemotePost).filter(RemotePost.source_id == self.source.id, RemotePost.original_id == content_id).one_or_none()
            
            if remote_post is None:
                self.log.info('creating new post')
                
                metadata = {}
                if content.plan is not None:
                    metadata['price'] = content.plan.price
                
                remote_post = RemotePost(
                    source=self.source,
                    original_id=content_id,
                    url=POST_FORMAT.format(post_id=post.id),
                    title=content.title,
                    comment=content.comment,
                    type=PostType.collection,
                    post_time=post_time,
                    metadata_=json.dumps(metadata)
                )
                
                if post.liked is True:
                    remote_post.favorite = True
                
                # creators are identified by their id because their name can change
                creator_tag = self.core.get_remote_tag(TagCategory.artist, creator_id)
                remote_post.tags.append(creator_tag)
                metadata = hoordu.Dynamic.from_json(creator_tag.metadata_)
                if metadata.get('name', None) != creator_name:
                    metadata.name = creator_name
                    creator_tag.metadata_ = metadata.to_json()
                    self.core.add(creator_tag)
                
                for tag in post.tags:
                    remote_tag = self.core.get_remote_tag(TagCategory.general, tag.name)
                    remote_post.tags.append(remote_tag)
                
                if post.rating == 'adult':
                    nsfw_tag = self.core.get_remote_tag(TagCategory.meta, 'nsfw')
                    remote_post.tags.append(nsfw_tag)
                
                self.core.add(remote_post)
        
        if content.category == 'file':
            if len(remote_post.files) == 0:
                file = File(remote=remote_post, remote_order=0, filename=content.filename)
                self.core.add(file)
                self.core.flush()
                self.log.info('found new file for post %s, filename: %s', remote_post.id, content.filename)
            else:
                file = remote_post.files[0]
            
            need_orig = not file.present and not preview
            need_thumb = not file.thumb_present
            if need_orig or need_thumb:
                self.log.info('downloading: %s, file: %r, thumb: %r', content.filename, need_orig, need_thumb)
                orig_url = FILE_DOWNLOAD_URL.format(download_uri=content.download_uri)
                orig = self._download_file(orig_url, filename=content.filename) if need_orig else None
                
                if post.thumb is not None:
                    thumb = self._download_file(post.thumb.medium) if need_thumb else None
                    
                else:
                    thumb = None
                
                self.core.import_file(file, orig=orig, thumb=thumb, move=True)
            
        elif content.category == 'photo_gallery':
            current_files = {file.remote_order: file for file in remote_post.files}
            
            for photo in content.post_content_photos:
                order = int(photo.id)
                file = current_files.get(order)
                
                if file is None:
                    file = File(remote=remote_post, remote_order=order)
                    self.core.add(file)
                    self.core.flush()
                    self.log.info('found new file for post %s, file order: %s', remote_post.id, order)
                
                need_orig = not file.present and not preview
                need_thumb = not file.thumb_present
                
                if need_thumb or need_orig:
                    self.log.info('downloading files for post: %s, order: %r', remote_post.id, file.remote_order)
                    
                    orig = self._download_file(photo.url.original) if need_orig else None
                    thumb = self._download_file(photo.url.medium) if need_thumb else None
                    
                    self.core.import_file(file, orig=orig, thumb=thumb, move=True)
            
        elif content.category == 'text':
            # there are no files to save
            remote_post.type = PostType.set
            self.core.add(remote_post)
            
        elif content.category == 'blog':
            current_files = {file.remote_order: file for file in remote_post.files}
            
            sections = hoordu.Dynamic.from_json(content.comment).ops
            blog = []
            for section in sections:
                insert = section.insert
                if isinstance(insert, str):
                    blog.append({
                        'type': 'text',
                        'content': insert
                    })
                    
                elif isinstance(insert, hoordu.Dynamic):
                    fantiaImage = insert.get('fantiaImage')
                    if fantiaImage is not None:
                        order = int(fantiaImage.id)
                        file = current_files.get(order)
                        
                        if file is None:
                            file = File(remote=remote_post, remote_order=order)
                            self.core.add(file)
                            self.core.flush()
                            self.log.info('found new file for post %s, file order: %s', remote_post.id, order)
                        
                        orig_url = FILE_DOWNLOAD_URL.format(download_uri=fantiaImage.original_url)
                        thumb_url = fantiaImage.url
                        
                        need_orig = not file.present and not preview
                        need_thumb = not file.thumb_present
                        
                        if need_thumb or need_orig:
                            self.log.info('downloading files for post: %s, order: %r', remote_post.id, file.remote_order)
                            
                            orig = self._download_file(orig_url) if need_orig else None
                            thumb = self._download_file(thumb_url) if need_thumb else None
                            
                            self.core.import_file(file, orig=orig, thumb=thumb, move=True)
                        
                        blog.append({
                            'type': 'file',
                            'order': order
                        })
                        
                    else:
                        self.log.warning('unknown blog insert: %s', str(insert))
            
            remote_post.comment = hoordu.Dynamic({'comment': blog}).to_json()
            remote_post.type = PostType.blog
            self.core.add(remote_post)
            
        else:
            raise ValueError('unknown content category: {}'.format(content.category))
        
        return remote_post
    
    def _to_remote_posts(self, post, remote_post=None, preview=False):
        main_id = str(post.id)
        creator_id = str(post.fanclub.id)
        creator_name = post.fanclub.user.name
        # possible timezone issues?
        post_time = dateutil.parser.parse(post.posted_at).astimezone(timezone.utc)
        
        self.log.info('getting post %s', main_id)
        
        if remote_post is not None:
            id_parts = remote_post.id.split('-')
            if len(id_parts) == 2:
                content_id = int(id_parts[1])
                
                content = next((c for c in post.post_contents if c.id == content_id), None)
                
                if content is not None and content.visible_status == 'visible':
                    return [self._content_to_post(post, content, remote_post, preview)]
                else:
                    return [remote_post]
        
        if remote_post is None:
            remote_post = self.session.query(RemotePost).filter(RemotePost.source_id == self.source.id, RemotePost.original_id == main_id).one_or_none()
            
            if remote_post is None:
                self.log.info('creating new post')
                remote_post = RemotePost(
                    source=self.source,
                    original_id=main_id,
                    url=POST_FORMAT.format(post_id=main_id),
                    title=post.title,
                    comment=post.comment,
                    type=PostType.collection,
                    post_time=post_time
                )
                
                if post.liked is True:
                    remote_post.favorite = True
                
                # creators are identified by their id because their name can change
                creator_tag = self.core.get_remote_tag(TagCategory.artist, creator_id)
                remote_post.tags.append(creator_tag)
                metadata = hoordu.Dynamic.from_json(creator_tag.metadata_)
                if metadata.get('name', None) != creator_name:
                    metadata.name = creator_name
                    creator_tag.metadata_ = metadata.to_json()
                    self.core.add(creator_tag)
                
                for tag in post.tags:
                    remote_tag = self.core.get_remote_tag(TagCategory.general, tag.name)
                    remote_post.tags.append(remote_tag)
                
                if post.rating == 'adult':
                    nsfw_tag = self.core.get_remote_tag(TagCategory.meta, 'nsfw')
                    remote_post.tags.append(nsfw_tag)
                
                
                self.core.add(remote_post)
        
        # download thumbnail if there is one
        if len(remote_post.files) == 0:
            if post.thumb is not None:
                file = File(remote=remote_post, remote_order=0)
                self.core.add(file)
                self.core.flush()
            else:
                file = None
        else:
            file = remote_post.files[0]
        
        if file is not None:
            need_orig = not file.present and not preview
            need_thumb = not file.thumb_present
            if need_orig or need_thumb:
                self.log.info('downloading files for post: %s, order: %r', remote_post.id, file.remote_order)
                orig = self._download_file(post.thumb.original) if need_orig else None
                thumb = self._download_file(post.thumb.medium) if need_thumb else None
                self.core.import_file(file, orig=orig, thumb=thumb, move=True)
        
        # convert the post contents to posts as well
        remote_posts = [remote_post]
        for content in post.post_contents:
            if content.visible_status == 'visible':
                content_post = self._content_to_post(post, content, preview=preview)
                remote_posts.append(content_post)
                self.core.flush()
                rel = self.session.query(Related).filter(Related.related_to_id == remote_post.id, Related.remote_id == content_post.id).one_or_none()
                if rel is None:
                    remote_post.related.append(Related(remote=content_post))
        
        return remote_posts
    
    def download(self, url=None, remote_post=None, preview=False):
        """
        Creates or updates a RemotePost entry along with all the associated Files,
        and downloads all files and thumbnails that aren't present yet.
        
        If remote_post is passed, its original_id will be used and it will be
        updated in place.
        
        If preview is set to True, then only the thumbnails are downloaded.
        
        Returns the downloaded RemotePost object.
        """
        
        if url is None and remote_post is None:
            raise ValueError('either url or remote_post must be passed')
        
        if remote_post is not None:
            post_id = remote_post.original_id.split('-')[0]
            self.log.info('update request for %s', post_id)
            
        else:
            self.log.info('download request for %s', url)
            if url.isdigit():
                post_id = url
                
            else:
                match = POST_REGEXP.match(url)
                if not match:
                    raise ValueError('unsupported url: {}'.format(repr(url)))
                
                post_id = match.group('post_id')
        
        response = self.http.get(POST_GET_URL.format(post_id=post_id))
        response.raise_for_status()
        post = hoordu.Dynamic.from_json(response.text).post
        self.log.debug('post json: %s', post)
        
        remote_posts = self._to_remote_posts(post, remote_post=remote_post, preview=preview)
        if remote_posts is not None and len(remote_posts) > 0:
            return remote_posts[0]
        else:
            return None
    
    def search_form(self):
        return Form('{} search'.format(self.name),
            ('creator_id', Input('fanclub id', [validators.required()]))
        )
    
    def search(self, options):
        """
        Creates a temporary search for a given set of search options.
        
        Returns a post iterator object.
        """
        
        return CreatorIterator(self, options=options)
    
    def create_subscription(self, name, options=None, iterator=None):
        """
        Creates a Subscription entry for the given search options identified by the given name,
        should not get any posts from the post source.
        """
        
        if iterator is not None:
            options = hoordu.Dynamic({
                'creator_id': iterator.creator_id
            })
            state = iterator.state
            
        elif options is not None:
            state = hoordu.Dynamic()
        
        sub = Subscription(
            source=self.source,
            name=name,
            options=options.to_json(),
            state=state.to_json()
        )
        
        self.core.add(sub)
        self.core.flush()
        
        if iterator is not None:
            iterator.subscription = sub
        
        return sub
    
    def get_iterator(self, subscription):
        """
        Gets the post iterator for a specific subscription.
        
        Returns a post iterator object.
        """
        
        return CreatorIterator(self, subscription=subscription)

Plugin = Fantia


