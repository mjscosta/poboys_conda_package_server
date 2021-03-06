#!/bin/env python

from bottle import default_app, get, post, template, request, static_file, redirect, abort
import os
from subprocess import call, Popen, PIPE, STDOUT
import argparse
import tempfile
import sys
import logging
import base64

log = logging.getLogger(__name__)
out_hdlr = logging.StreamHandler(sys.stdout)
out_hdlr.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
out_hdlr.setLevel(logging.INFO)
log.addHandler(out_hdlr)
log.setLevel(logging.INFO)

# need to parse args right away because decorators depend on args
prefix = None
s3_bucket = None
anaconda_release = False
anaconda_user = None
anaconda_password = None
anaconda_org = None
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, help="port to listen on")
parser.add_argument("--s3_bucket", help="S3 bucket to sync with")
parser.add_argument("--prefix", help="Prefix to also serve webpage at, i.e. www.example.com and www.example.com/prefix/")
parser.add_argument("--ac_user", help="Anaconda user name" )
parser.add_argument("--ac_pass", help="Anaconda user name" )
parser.add_argument("--ac_org", help="Anaconda user name" )

args = parser.parse_args()

if not args.port:
    args.port = 6969
if args.s3_bucket:
    import boto3
    s3_bucket = args.s3_bucket
if args.prefix:
    if not args.prefix.startswith('/'):
        args.prefix = '/' + args.prefix
    if args.prefix.endswith('/'):
        args.prefix = args.prefix[:-1]
    prefix = args.prefix
else:
    prefix = '/poboys'
if args.ac_user and args.ac_pass:
    anaconda_user = args.ac_user
    anaconda_password = args.ac_pass
    anaconda_release = True
if args.ac_org:
    anaconda_org = args.ac_org


platforms = ['noarch', 'linux-64', 'win-64', 'osx-64', 'linux-ppc64le']


def ensure_pkgs_dir_exists():
    try: 
        os.makedirs('pkgs')
    except OSError:
        if not os.path.isdir('pkgs'):
            raise

    return 'pkgs'


def ensure_platform_dir_exists(platform):
    pkgs_dir = ensure_pkgs_dir_exists()
    platform_dir = os.path.join(pkgs_dir, platform)

    try: 
        if platform not in platforms:
            abort(404, "Invalid platform %s" % platform)
        os.makedirs(platform_dir)
    except OSError:
        if not os.path.isdir(platform_dir):
            raise

    return platform_dir


def reindex_platform_dir(platform_dir):
    savedir = os.getcwd()
    os.chdir(platform_dir)
    call(["conda", "index"])
    os.chdir(savedir)
    return ['repodata.json', 'repodata.json.bz2', '.index.json']


@get('/')
@get(prefix + '/')
def index():
    return template('index', prefix=prefix, platforms=platforms)


@post('/upload')
@post(prefix + '/upload')
def do_upload():
    platform = request.forms.get('platform')
    fileupload = request.files.get('fileupload')
    filename = fileupload.filename

    platform_dir = ensure_platform_dir_exists(platform)

    fileupload.save(platform_dir, overwrite=False)
    index_filenames = reindex_platform_dir(platform_dir)

    # upload to S3 if requested
    if s3_bucket:
        try:
            s3 = boto3.resource('s3')
            with open(os.path.join(platform_dir, filename), 'rb') as f:
                s3.Object(s3_bucket, os.path.join(platform, filename)).put(Body=f)
            for index_filename in index_filenames:
                with open(os.path.join(platform_dir, index_filename), 'rb') as f:
                    s3.Object(s3_bucket, os.path.join(platform, index_filename)).put(Body=f)
        except Exception as e:
            # something went wrong.  Undo everything and bail
            os.remove(os.path.join(platform_dir, filename))
            reindex_platform_dir(platform_dir)
            abort(503, "Failed to upload to S3 %s with exception %s" % (s3_bucket, str(e)))

    redirect(prefix + '/pkgs/' + platform + '?' + 'message={filename} uploaded'.format(filename=filename))


@get('/pkgs')
@get(prefix + '/pkgs')
def get_pkgs():
    pkgs_dir = ensure_pkgs_dir_exists()
    message = request.query.message
    filelist = sorted([ f for f in os.listdir(pkgs_dir) ])
    return template('filelist_to_links',
                    header='Platforms',
                    prefix=prefix,
                    parenturl='/pkgs',
                    filelist=filelist,
                    allow_delete=False,
                    anaconda_release=False,
                    message=message)


@get('/pkgs/<platform>')
@get(prefix + '/pkgs/<platform>')
def get_platform(platform):
    if not platform in platforms:
        return "Unknown platform " + platform

    platform_dir = ensure_platform_dir_exists(platform)
    message = request.query.message

    filelist = sorted([ f for f in os.listdir(platform_dir) ])
    return template('filelist_to_links',
                    header='Packages',
                    prefix=prefix,
                    parenturl='/pkgs/'+platform,
                    filelist=filelist,
                    allow_delete=True,
                    anaconda_release=anaconda_release,
                    message=message)


@get('/pkgs/<platform>/<filename>')
@get(prefix + '/pkgs/<platform>/<filename>')
def get_file(platform, filename):
    if not platform in platforms:
        return "Unknown platform " + platform

    platform_dir = ensure_platform_dir_exists(platform)

    return static_file(filename, root=platform_dir, download=filename)


@post('/delete/pkgs/<platform>/<filename>')
@post(prefix + '/delete/pkgs/<platform>/<filename>')
def del_file(platform, filename):
    if not platform in platforms:
        return "Unknown platform " + platform

    platform_dir = ensure_platform_dir_exists(platform)
    tempdir = tempfile.gettempdir()

    try:
        # move to a tempdir (in case we need to undo this)
        os.remove(os.path.join(platform_dir, filename))
    except OSError:
        pass

    index_filenames = reindex_platform_dir(platform_dir)

    # delete from S3 if requested
    if s3_bucket:
        try:
            s3 = boto3.resource('s3')
            s3.Object(s3_bucket, os.path.join(platform, filename)).delete()
            for index_filename in index_filenames:
                with open(os.path.join(platform_dir, index_filename), 'rb') as f:
                    s3.Object(s3_bucket, os.path.join(platform, index_filename)).put(Body=f)
        except Exception as e:
            # something went wrong.  Undo everything and bail
            os.rename(os.path.join(tempdir, filename), os.path.join(platform_dir, filename))
            reindex_platform_dir(platform_dir) 
            abort(503, "Failed to delete from S3 bucket %s with exception %s" % (s3_bucket, str(e)))

    # commit the delete
    # os.remove(os.path.join(tempdir, filename))

    redirect(prefix + '/pkgs/' + platform)

@post('/anaconda/release/pkgs/<platform>/<filename>')
@post(prefix + '/anaconda/release/pkgs/<platform>/<filename>')
def release_file(platform, filename):
    """
    Releases/Uploads file to anaconda cloud. By default, uploads to user's account. If organization
    is specified, then upload to the org instead.
    :param platform: released platform e.g. linux-64, linux-ppc64le etc.
    :param filename: conda package file (tar.bz2) to be released to anaconda cloud 
    :return: 
    """
    if not platform in platforms:
        return "Unknown platform " + platform
    ensure_platform_dir_exists(platform)

    upload_commands = """
    anaconda login --username {username} --password {password} &&
    anaconda upload --no-progress -u {org} {file} &&
    anaconda logout
    """.format(username=anaconda_user,
               password=anaconda_password,
               org=anaconda_org or anaconda_user,
               file=filename)
    os.chdir('pkgs/'+platform)
    cwd = os.getcwd()
    p = Popen(upload_commands, shell=True, stdin=PIPE, stdout=PIPE, stderr=STDOUT, close_fds=True)
    stdout_data, stderr_data = p.communicate()
    os.chdir('../../')
    redirect(prefix + '/pkgs/' + platform + '?' + 'message={msg}'.format(msg=base64.urlsafe_b64encode(stdout_data).decode('ascii').rstrip()))


if __name__ == '__main__':
    for platform in platforms:
        ensure_platform_dir_exists(platform)
        os.chdir('pkgs/'+platform)
        call(["conda", "index"])
        os.chdir('../../')

    app = default_app()

    log.info("Serving on port %d" % args.port)
    app.run(host='0.0.0.0', port=args.port, debug=True)
