import os
import sys
import subprocess
import urllib
import glob
import shutil
import urlparse
import simplejson
import datetime
import smhasher
import logging
import robotparser
import re
import time
from random import choice


from djcelery import celery
import requests
from django.conf import settings

from perma.models import Asset, Stat, Registrar, LinkUser, Link, VestingOrg
from perma.exceptions import BrokenURLError
from perma.settings import INSTAPAPER_KEY, INSTAPAPER_SECRET, INSTAPAPER_USER, INSTAPAPER_PASS, GENERATED_ASSETS_STORAGE

import oauth2 as oauth


logger = logging.getLogger(__name__)

@celery.task
def start_proxy_record(link_guid, target_url, base_storage_path):
    """
    start warcprox process. Warcprox is a MITM proxy server and needs to be running 
    before, during and after phantomjs gets a screenshot.
    """
    port_list = range(27500, 27900)
    path_elements = [settings.GENERATED_ASSETS_STORAGE, base_storage_path]
    print os.path.sep.join(path_elements)

    if not os.path.exists(os.path.sep.join(path_elements)):
        os.makedirs(os.path.sep.join(path_elements))

    #### TODO if warcprox is called using the same port as an existing warcprox process, a socket.error with be thrown in the subprocess. For prototyping, it's okay to have a port choosen at random out of a range of 400.


    prox_port = str(choice(port_list)) #select a random port
    warcprox_server = subprocess.Popen([  "python",
                                          "-m",
                                          "warcprox.warcprox",
                                          "--prefix=%s" % ("permaWarcFile"),
                                          #"--gzip", 
                                          "--dir=%s" % (os.path.sep.join(path_elements)), 
                                          "--certs-dir=%s" % (os.path.sep.join(path_elements)), 
                                          "--port=%s" % (prox_port), 
                                          "--dedup-db-file=/dev/null", 
                                          "--address=%s" % ("127.0.0.1"),
                                          "--rollover-idle-time=15",
                                          "--quiet"],
                                          cwd=os.path.sep.join(path_elements),
                                          stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE,
                                          )
    time.sleep(0.3) # warcprox needs time to setup

    return (warcprox_server, prox_port) # return the process while it is running. The process is passed to get_screen_cap so that it can be terminated after phantomjs finishes. 

@celery.task
def get_screen_cap(link_guid, target_url, base_storage_path, warcprox_comm ,user_agent=''):
    """
    Create an image from the supplied URL, write it to disk and update our asset model with the path.
    The heavy lifting is done by PhantomJS, our headless browser.

    This task also handles the teardown for start_proxy_record by terminating warcprox and 


    This function is usually executed via a synchronous Celery call
    """

    warcprox_subprocess = warcprox_comm[0] # the warcprox process subprocess from start_proxy_record
    warcprox_port = warcprox_comm[1] # the port warcprox is listening on from start_proxy_record

    path_elements = [settings.GENERATED_ASSETS_STORAGE, base_storage_path, 'cap.png']
    
    if not os.path.exists(os.path.sep.join(path_elements[:2])):
        os.makedirs(os.path.sep.join(path_elements[:2]))

    ### warcprox generates an encryption certificate which can be passed to phantomjs. The nameing convention is <system name>-warcprox-ca.pem. 
    cert_path = "--ssl-certificates-path="+os.path.sep.join(path_elements[:2])+"/"+filter(lambda x : "warcprox-ca.pem" in x ,os.listdir(os.path.sep.join(path_elements[:2])))[0]
    
    try:
        image_generation_command = settings.PROJECT_ROOT + '/lib/phantomjs ' +"--proxy=127.0.0.1:"+warcprox_port +" "+cert_path+" "+"--ignore-ssl-errors=true " + settings.PROJECT_ROOT+'/lib/rasterize.js "' +target_url+'" ' + os.path.sep.join(path_elements) +' "' + user_agent + '"'

        phantomcall = subprocess.call(image_generation_command, shell=True)
        time.sleep(0.3)
    finally: # shutdown warcprox process
        warcprox_subprocess.terminate() # send term signal to warcprox 
        time.sleep(0.2) # warcprox needs time to properly shut down


    if os.path.exists(os.path.sep.join(path_elements)):
        asset = Asset.objects.get(link__guid=link_guid)
        asset.image_capture = "/"+os.path.sep.join(path_elements[2:])
        asset.save()
    else:
        logger.info("Screen capture failed for %s" % target_url)
        asset = Asset.objects.get(link__guid=link_guid)
        asset.image_capture = 'failed'
        asset.save()
        logger.info("Screen capture failed for %s" % target_url)

    ### Handles the warc created by warcprox
    warc_path_elements = os.path.sep.join(path_elements[0:2])

    if os.path.exists(warc_path_elements):
        test_for_closed_warc = lambda x: "permaWarcFile" in x and ".open" not in x
        for retrys in range(0, 10):
            created_warc_name = filter(test_for_closed_warc ,os.listdir(warc_path_elements)) #list all files in the base storage path and return the name of the file warcprox generated. Do not return if the file is still open.
            if len(created_warc_name) == 0:
                time.sleep(0.5) 
                continue
            else:
                break
        print "-----------", created_warc_name
        standardized_warc_name = os.path.join(warc_path_elements,"archive.warc")
        os.rename(os.path.join(warc_path_elements, created_warc_name[0]), standardized_warc_name)
        #warc_path_elements = (os.path.join(path_elements[0:2])).append("archive.warc.gz")
        asset = Asset.objects.get(link__guid=link_guid)
        asset.warc_capture = "archive.warc"
        asset.save()
    else:
        logger.info("Web Archive File creation failed for %s" % target_url)
        asset = Asset.objects.get(link__guid=link_guid)
        asset.warc_capture = 'failed'
        asset.save()
        logger.info("Web Archive File creation failed for %s" % target_url)

@celery.task
def get_source(link_guid, target_url, base_storage_path, user_agent=''):
    """
    Download the source that is used to generate the page at the supplied URL.
    Assets are written to disk, in a directory called "source". If things go well, we update our
    assets model with the path.
    We use a robust wget command for this.

    This function is usually executed via an asynchronous Celery call
    """

    path_elements = [settings.GENERATED_ASSETS_STORAGE, base_storage_path, 'source', 'index.html']

    directory = os.path.sep.join(path_elements[:3])

    headers = {
        #'Accept': ','.join(settings.ACCEPT_CONTENT_TYPES),
        #'User-Agent': user_agent,
    }

    """ Get the markup and assets, update our db, and write them to disk """
    # Construct wget command
    command = 'wget '
    command = command + '--quiet ' # turn off wget's output
    command = command + '--tries=' + str(settings.NUMBER_RETRIES) + ' ' # number of retries (assuming no 404 or the like)
    command = command + '--wait=' + str(settings.WAIT_BETWEEN_TRIES) + ' ' # number of seconds between requests (lighten the load on a page that has a lot of assets)
    command = command + '--quota=' + settings.ARCHIVE_QUOTA + ' ' # only store this amount
    command = command + '--random-wait ' # random wait between .5 seconds and --wait=
    command = command + '--limit-rate=' + settings.ARCHIVE_LIMIT_RATE  + ' ' # we'll be performing multiple archives at once. let's not download too much in one stream
    command = command + '--adjust-extension '  # if a page is served up at .asp, adjust to .html. (this is the new --html-extension flag)
    command = command + '--span-hosts ' # sometimes things like images are hosted at a CDN. let's span-hosts to get those
    command = command + '--convert-links ' # rewrite links in downloaded source so they can be viewed in our local version
    command = command + '-e robots=off ' # we're not crawling, just viewing the page exactly as you would in a web-browser.
    command = command + '--page-requisites ' # get the things required to render the page later. things like images.
    command = command + '--no-directories ' # when downloading, flatten the source. we don't need a bunch of dirs.
    command = command + '--no-check-certificate ' # We don't care too much about busted certs
    command = command + '--user-agent="' + user_agent + '" ' # pass through our user's user agent
    command = command + '--directory-prefix=' + directory + ' ' # store our downloaded source in this directory

    # Add headers (defined earlier in this function)
    for key, value in headers.iteritems():
        command = command + '--header="' + key + ': ' + value + '" '

    command = command + target_url

    # Download page data and dependencies
    if not os.path.exists(directory):
        os.makedirs(directory)

    #TODO replace os.popen with subprocess
    output = os.popen(command)

    # Verify success
    if '400 Bad Request' in output:
        logger.info("Source capture failed for %s" % target_url)
        asset = Asset.objects.get(link__guid=link_guid)
        asset.warc_capture = 'failed'
        asset.save()

    filename = urllib.unquote(target_url.split('/')[-1]).decode('utf8')
    if filename != '' and 'index.html' not in os.listdir(directory):
        try:
            src = os.path.join(directory, filename)
            des = os.path.sep.join(path_elements)
            shutil.move(src, des)
        except:
            # Rename the file as index.html if it contains '<html'
            counter = 0
            for filename in glob.glob(directory + '/*'):
                with open(filename) as f:
                    if '<html' in f.read():
                        shutil.move(os.path.join(directory, filename), os.path.sep.join(path_elements))
                        counter = counter + 1
            if counter == 0:
                # If we still don't have an index.html file, raise an exception and record it to the DB
                asset = Asset.objects.get(link__guid=link_guid)
                asset.warc_capture = 'failed'
                asset.save()

                logger.info("Source capture got some content, but couldn't rename to index.html for %s" % target_url)
                os.system('rm -rf ' + directory)

    if os.path.exists(os.path.sep.join(path_elements)):
        asset = Asset.objects.get(link__guid=link_guid)
        asset.warc_capture = os.path.sep.join(path_elements[2:])
        asset.save()
    else:
        logger.info("Source capture failed for %s" % target_url)
        asset = Asset.objects.get(link__guid=link_guid)
        asset.warc_capture = 'failed'
        asset.save()


@celery.task
def get_pdf(link_guid, target_url, base_storage_path, user_agent):
    """
    Dowload a PDF from the network

    This function is usually executed via a synchronous Celery call
    """
    asset = Asset.objects.get(link__guid=link_guid)
    asset.pdf_capture = 'pending'
    asset.save()
    
    path_elements = [settings.GENERATED_ASSETS_STORAGE, base_storage_path, 'cap.pdf']

    if not os.path.exists(os.path.sep.join(path_elements[:2])):
        os.makedirs(os.path.sep.join(path_elements[:2]))

    # Get the PDF from the network
    headers = {
        'User-Agent': user_agent,
    }
    r = requests.get(target_url, stream = True, headers=headers)
    file_path = os.path.sep.join(path_elements)

    try:
        with open(file_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
            
                # Limit our filesize
                if f.tell() > settings.MAX_ARCHIVE_FILE_SIZE:
                    raise
                
                if chunk: # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()
                    
    except Exception, e:
        logger.info("PDF capture too big, %s" % target_url)
        os.remove(file_path)

    if os.path.exists(os.path.sep.join(path_elements)):
        # TODO: run some sort of validation check on the PDF
        asset.pdf_capture = os.path.sep.join(path_elements[2:])
        asset.save()
    else:
        logger.info("PDF capture failed for %s" % target_url)
        asset.pdf_capture = 'failed'
        asset.save()


def instapaper_capture(url, title):
    consumer = oauth.Consumer(INSTAPAPER_KEY, INSTAPAPER_SECRET)
    client = oauth.Client(consumer)

    resp, content = client.request('https://www.instapaper.com/api/1/oauth/access_token', "POST", urllib.urlencode({
                'x_auth_mode': 'client_auth',
                'x_auth_username': INSTAPAPER_USER,
                'x_auth_password': INSTAPAPER_PASS,
                }))

    token = dict(urlparse.parse_qsl(content))
    token = oauth.Token(token['oauth_token'], token['oauth_token_secret'])
    http = oauth.Client(consumer, token)

    response, data = http.request('https://www.instapaper.com/api/1/bookmarks/add', method='POST', body=urllib.urlencode({'url':url, 'title': unicode(title).encode('utf-8')}))

    res = simplejson.loads(data)

    bid = res[0]['bookmark_id']

    tresponse, tdata = http.request('https://www.instapaper.com/api/1/bookmarks/get_text', method='POST', body=urllib.urlencode({'bookmark_id':bid}))

    # If didn't get a response or we got something other than an HTTP 200, count it as a failure
    success = True
    if not tresponse or tresponse.status != 200:
        success = False
    
    return bid, tdata, success


@celery.task
def store_text_cap(url, title, link_guid):
    
    bid, tdata, success = instapaper_capture(url, title)
    
    if success:
        asset = Asset.objects.get(link__guid=link_guid)
        asset.instapaper_timestamp = datetime.datetime.now()
        h = smhasher.murmur3_x86_128(tdata)
        asset.instapaper_hash = h
        asset.instapaper_id = bid
        asset.save()
    
        file_path = GENERATED_ASSETS_STORAGE + '/' + asset.base_storage_path
        if not os.path.exists(file_path):
            os.makedirs(file_path)

        f = open(file_path + '/instapaper_cap.html', 'w')
        f.write(tdata)
        os.fsync(f)
        f.close

        if os.path.exists(file_path + '/instapaper_cap.html'):
            asset.text_capture = 'instapaper_cap.html'
            asset.save()
        else:
            logger.info("Text (instapaper) capture failed for %s" % target_url)
            asset.text_capture = 'failed'
            asset.save()
    else:
        # Must have received something other than an HTTP 200 from Instapaper, or no response object at all
        logger.info("Text (instapaper) capture failed for %s" % target_url)
        asset = Asset.objects.get(link__guid=link_guid)
        asset.text_capture = 'failed'
        asset.save()


@celery.task
def get_robots_txt(url, link_guid):
    """
    A task (hopefully called asynchronously) to get the robots.txt rule for PermaBot.
    We will still grab the content (we're not a crawler), but we'll "darchive it."
    """
    
    # Parse the URL so and build the robots.txt location
    parsed_url = urlparse.urlparse(url)
    robots_text_location = parsed_url.scheme + '://' + parsed_url.netloc + '/robots.txt'
    
    # We only want to respect robots.txt if PermaBot is specifically asked not crawl (we're not a crawler)
    response = requests.get(robots_text_location)
    
    # We found PermaBot specifically mentioned
    if re.search('PermaBot', response.text) is not None:
        # Get the robots.txt ruleset
        # TODO: use reppy or something else here. it's dumb that we're
        # getting robots.txt twice
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_text_location)
        rp.read()

        # If we're not allowed, set a flag in the model
        if not rp.can_fetch('PermaBot', url):
            link = Link.objects.get(guid=link_guid)
            link.dark_archived_robots_txt_blocked = True
            link.save()


    
@celery.task
def get_nigthly_stats():
    """
    A periodic task (probably running nightly) to get counts of user types, disk usage.
    Write them into a DB for our stats view
    """
    
    # Five types user accounts
    total_count_regular_users = LinkUser.objects.filter(groups__name='user').count()
    total_count_vesting_members = LinkUser.objects.filter(groups__name='vesting_member').count()
    total_count_vesting_managers = LinkUser.objects.filter(groups__name='vesting_manager').count()
    total_count_registrar_members = LinkUser.objects.filter(groups__name='registrar_member').count()
    total_count_registry_members = LinkUser.objects.filter(groups__name='registry_member').count()
    
    # Registrar count
    total_count_registrars = Registrar.objects.all().count()
    
    # Journal account
    total_vesting_orgs = VestingOrg.objects.all().count()
    
    # Two types of links
    total_count_unvested_links = Link.objects.filter(vested=False).count()
    total_count_vested_links = Link.objects.filter(vested=True).count()
    
    # Get things in the darchive
    total_count_darchive_takedown_links = Link.objects.filter(dark_archived=True).count()
    total_count_darchive_robots_links = Link.objects.filter(dark_archived_robots_txt_blocked=True).count()
    
    # Get the path of yesterday's file storage tree
    now = datetime.datetime.now() - datetime.timedelta(days=1)
    time_tuple = now.timetuple()
    path_elements = [str(time_tuple.tm_year), str(time_tuple.tm_mon), str(time_tuple.tm_mday)]
    disk_path = settings.GENERATED_ASSETS_STORAGE + '/' + os.path.sep.join(path_elements)
    
    # Get disk usage total
    # If we start deleting unvested archives, we'll have to do some periodic corrrections here (this is only additive)
    # Get the sum of the diskspace of all files in yesterday's tree
    latest_day_usage = 0
    for root, dirs, files in os.walk(disk_path):
        latest_day_usage = latest_day_usage + sum(os.path.getsize(os.path.join(root, name)) for name in files)
        
    # Get the total disk usage (that we calculated yesterday)
    stat = Stat.objects.all().order_by('-creation_timestamp')[:1]
    
    # Sum total usage with yesterday's usage
    new_total_disk_usage = stat[0].disk_usage + latest_day_usage
    
    # We've now gathered all of our data. Let's write it to the model
    stat = Stat(
        regular_user_count=total_count_regular_users,
        vesting_member_count=total_count_vesting_members,
        vesting_manager_count=total_count_vesting_managers,
        registrar_member_count=total_count_registrar_members,
        registry_member_count=total_count_registry_members,
        registrar_count=total_count_registrars,
        vesting_org_count=total_vesting_orgs,
        unvested_count=total_count_unvested_links,
        darchive_takedown_count = total_count_darchive_takedown_links,
        darchive_robots_count = total_count_darchive_robots_links,
        vested_count=total_count_vested_links,
        disk_usage = new_total_disk_usage,
        )

    stat.save()