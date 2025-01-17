import os
import sys
import uuid
import base64
import urllib
import logging
import tempfile
import subprocess

import boto3
import click

from botocore.exceptions import ClientError, NoRegionError


session_id = uuid.uuid4().hex
DEFAULT_INSTANCE_TYPE = "t2.micro"
DEFAULT_REGION = 'eu-west-1'
INSTANCE_AMI = 'ami-a8d2d7ce'
MY_IP = urllib.urlopen('http://whatismyip.org').read()

# Keypair prepiration
INST_KEYPAIR = open('{}/{}'.format(tempfile.gettempdir(), session_id), 'w+')
KEYPAIR_PATH = os.path.join(tempfile.gettempdir(), session_id)
os.chmod(KEYPAIR_PATH, 0600)

# Userdata for the AWS instance
USERDATA = """#!/bin/bash
set -x
sleep 10
echo "#!/bin/bash\nif who | wc -l | grep -q 1 ; then shutdown -h +5 'Server Idle, Server termination' ; fi" > /root/inst_linux.sh
chmod +x /root/inst_linux.sh
echo "*/15 * * * * root /root/inst_linux.sh" >> /root/mycron
crontab /root/mycron
echo export TMOUT=300 >> /etc/environment"""

# Setting up a logger
logger = logging.getLogger('inst')
logger.setLevel(logging.INFO)
console = logging.StreamHandler()
logger.addHandler(console)


def aws_client(resource=True, aws_service='ec2'):
    try:
        if resource:
            return boto3.resource(aws_service)
        else:
            return boto3.client(aws_service)
    except NoRegionError as e:
        logger.warning("Error reading 'Default Region'. Make sure boto is configured")
        sys.exit()

def create_security_group():
    try:
        mysg = aws_client().create_security_group(
            GroupName="INST_LINUX", Description='Single serving SG')
        mysg.authorize_ingress(IpProtocol="tcp", CidrIp='0.0.0.0/0'.format(
            MY_IP), FromPort=22, ToPort=22)
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
            logger.debug("SG exists - Skipping")
            pass
    return "INST_LINUX"

def keypair():
    keypair = aws_client(resource=False).create_key_pair(KeyName=session_id)
    INST_KEYPAIR.write(keypair['KeyMaterial'])
    INST_KEYPAIR.close()
    return session_id

def get_spot_info(spotid):
    client = aws_client(resource=False)
    spot_status = client.describe_spot_instance_requests(SpotInstanceRequestIds=[spotid])
    return spot_status["SpotInstanceRequests"][0]

def get_spot_price(type):
    client = aws_client(resource=False)
    return client.describe_spot_price_history(InstanceTypes=[type],MaxResults=1,ProductDescriptions=['Linux/UNIX (Amazon VPC)'])["SpotPriceHistory"][0]["SpotPrice"]

def start_instance(spot=False):
    if spot:
        client = aws_client(resource=False)
        LaunchSpecifications = {
            "ImageId": INSTANCE_AMI,
            "InstanceType": DEFAULT_INSTANCE_TYPE,
            "KeyName": keypair(),
            "UserData": base64.b64encode(USERDATA.encode("ascii")).\
                decode('ascii'),
            "SecurityGroupIds": ["sg-b247c9ca"]
        }
        spot_instance = client.request_spot_instances(
            SpotPrice=get_spot_price(DEFAULT_INSTANCE_TYPE),
            Type="one-time",
            InstanceCount=1,
            LaunchSpecification=LaunchSpecifications)
        SpotId = spot_instance["SpotInstanceRequests"][0]["SpotInstanceRequestId"]
    else:
        client = aws_client()
        instance = client.create_instances(
            ImageId=INSTANCE_AMI,
            MinCount=1,
            MaxCount=1,
            InstanceType=DEFAULT_INSTANCE_TYPE,
            KeyName=keypair(),
            UserData=USERDATA,
            SecurityGroups=[create_security_group()],
            InstanceInitiatedShutdownBehavior='terminate')[0]
    
    if not spot:
        logger.info('Waiting for instance to boot...')
    else:
        while get_spot_info(SpotId)["Status"]["Code"] != "fulfilled":
            print "Waiting for spot approval..."
        instance = aws_client().Instance(id=get_spot_info(SpotId)["InstanceId"])
    
    instance.wait_until_running()
    instance.load()
    return instance.public_dns_name


CLICK_CONTEXT_SETTINGS = dict(
    help_option_names=['-h', '--help'],
    token_normalize_func=lambda param: param.lower(),
    ignore_unknown_options=True)


@click.command(context_settings=CLICK_CONTEXT_SETTINGS)
@click.option('-s',
              '--ssh',
              is_flag=True,
              help='Do you want to connect to your instance?')
@click.option('--spot',
              is_flag=True,
              help='Do you want a spot instance type?')
@click.option('-v',
              '--verbose',
              is_flag=True,
              help="display run log in verbose mode")
def inst(ssh, spot, verbose):
    """Get a Linux distro instance on AWS with one click
    """
    # TODO: Handle error when instance creation failed.
    if verbose:
        logger.setLevel(logging.DEBUG)
    if ssh:
        if spot:
            ssh = subprocess.Popen(['ssh', '-i', KEYPAIR_PATH, '-o',
                                    'StrictHostKeychecking=no',
                                    'ubuntu@{}'.format(start_instance(spot=True))],
                                stderr=subprocess.PIPE)
            if "Operation timed out" in ssh.stderr.readlines()[0]:
                logging.warning("Could not connect to Instance")
        else:
            ssh = subprocess.Popen(['ssh', '-i', KEYPAIR_PATH, '-o',
                                    'StrictHostKeychecking=no',
                                    'ubuntu@{}'.format(start_instance())],
                                stderr=subprocess.PIPE)
            if "Operation timed out" in ssh.stderr.readlines()[0]:
                logging.warning("Could not connect to Instance")
    else:
        if spot:
            instance_address = start_instance(spot=True)
        else:
            instance_address = start_instance()
        print "To connect to your instance:"
        print "ssh -i {} ubuntu@{}".format(KEYPAIR_PATH, instance_address)
        