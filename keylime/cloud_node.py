#!/usr/bin/python

'''
DISTRIBUTION STATEMENT A. Approved for public release: distribution unlimited.

This material is based upon work supported by the Assistant Secretary of Defense for 
Research and Engineering under Air Force Contract No. FA8721-05-C-0002 and/or 
FA8702-15-D-0001. Any opinions, findings, conclusions or recommendations expressed in this
material are those of the author(s) and do not necessarily reflect the views of the 
Assistant Secretary of Defense for Research and Engineering.

Copyright 2015 Massachusetts Institute of Technology.

The software/firmware is provided to you on an As-Is basis

Delivered to the US Government with Unlimited Rights, as defined in DFARS Part 
252.227-7013 or 7014 (Feb 2014). Notwithstanding any copyright notice, U.S. Government 
rights in this work are defined by DFARS 252.227-7013 or DFARS 252.227-7014 as detailed 
above. Use of this work other than as specifically authorized by the U.S. Government may 
violate any copyrights that exist in this work.
'''

import common
logger = common.init_logging('cloudnode')


import BaseHTTPServer
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn
import threading
from urlparse import urlparse
import json
import base64
import ConfigParser
import uuid
import crypto
import os
import sys
import tpm_quote
import tpm_initialize
import registrar_client
import tpm_nvram
import secure_mount
import time
import hashlib
import openstack
import zipfile
import cStringIO
import revocation_notifier
import importlib
import shutil
import tpm_random
import tpm_exec

# read the config file
config = ConfigParser.RawConfigParser()
config.read(common.CONFIG_FILE)

#lock required for multithreaded operation
uvLock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    parsed_path = '' 
    
    def do_HEAD(self):
        """Not supported.  Will always return a 400 response"""
        self.do_GET()

    def do_GET(self):
        """This method services the GET request typically from either the Tenant or the Cloud Verifier.
        
        Only tenant and cloudverifier uri's are supported. Both requests require a nonce parameter.  
        The Cloud verifier requires an additional mask paramter.  If the uri or parameters are incorrect, a 400 response is returned.
        """
        
        logger.info('GET invoked from ' + str(self.client_address)  + ' with uri:' + self.path)
        
        rest_params = common.get_restful_params(self.path)
        if "keys" in rest_params and rest_params['keys']=='verify':
            if self.server.K is None:
                logger.info('GET key challenge returning 400 response. bootstrap key not available')
                common.echo_json_response(self, 400, "Bootstrap key not yet available.")
                return
            challenge = rest_params['challenge']
            response={}
            response['hmac'] = crypto.do_hmac(self.server.K, challenge)            
            common.echo_json_response(self, 200, "Success", response)
            logger.info('GET key challenge returning 200 response.')
            
        # If node pubkey requested
        elif "keys" in rest_params and rest_params["keys"] == "pubkey":
            response = {}
            response['pubkey'] = self.server.rsapublickey_exportable
            
            common.echo_json_response(self, 200, "Success", response)
            logger.info('GET pubkey returning 200 response.')
            return
        
        elif "quotes" in rest_params:
            nonce = rest_params['nonce']
            pcrmask = rest_params['mask'] if 'mask' in rest_params else None
            vpcrmask = rest_params['vmask'] if 'vmask' in rest_params else None
            
            # if the query is not messed up
            if nonce is None:
                logger.warning('GET quote returning 400 response. nonce not provided as an HTTP parameter in request')
                common.echo_json_response(self, 400, "nonce not provided as an HTTP parameter in request")
                return
            
            # Sanitization assurance (for tpm_exec.run() tasks below) 
            if not (nonce.isalnum() and (pcrmask is None or pcrmask.isalnum()) and (vpcrmask is None or vpcrmask.isalnum())):
                logger.warning('GET quote returning 400 response. parameters should be strictly alphanumeric')
                common.echo_json_response(self, 400, "parameters should be strictly alphanumeric")
                return
            
            # identity quotes are always shallow
            if not tpm_initialize.is_vtpm() or rest_params["quotes"]=='identity':
                quote = tpm_quote.create_quote(nonce, self.server.rsapublickey_exportable,pcrmask)
                imaMask = pcrmask
            else:
                quote = tpm_quote.create_deep_quote(nonce, self.server.rsapublickey_exportable, vpcrmask, pcrmask)
                imaMask = vpcrmask
            
            # Allow for a partial quote response (without pubkey) 
            if "partial" in rest_params and (rest_params["partial"] is None or int(rest_params["partial"],0) == 1):
                response = { 
                    'quote': quote, 
                    }
            else:
                response = {
                    'quote': quote, 
                    'pubkey': self.server.rsapublickey_exportable, 
                }
            
            # return a measurement list if available
            if tpm_quote.check_mask(imaMask, common.IMA_PCR):
                if not os.path.exists(common.IMA_ML):
                    logger.warn("IMA measurement list not available: %s"%(common.IMA_ML))
                else:
                    with open(common.IMA_ML,'r') as f:
                        ml = f.read()
                    response['ima_measurement_list']=ml
            
            common.echo_json_response(self, 200, "Success", response)
            logger.info('GET %s quote returning 200 response.'%(rest_params["quotes"]))
            return
        
        else:
            logger.warning('GET returning 400 response. uri not supported: ' + self.path)
            common.echo_json_response(self, 400, "uri not supported")
            return
        

    def do_POST(self):
        """This method services the POST request typically from either the Tenant or the Cloud Verifier.
        
        Only tenant and cloudverifier uri's are supported. Both requests require a nonce parameter.  
        The Cloud verifier requires an additional mask parameter.  If the uri or parameters are incorrect, a 400 response is returned.
        """        
        rest_params = common.get_restful_params(self.path)
        
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length <= 0:
            logger.warning('POST returning 400 response, expected content in message. url:  ' + self.path)
            common.echo_json_response(self, 400, "expected content in message")
            return
        
        post_body = self.rfile.read(content_length)
        json_body = json.loads(post_body)
            
        b64_encrypted_key = json_body['encrypted_key']
        decrypted_key = crypto.rsa_decrypt(self.server.rsaprivatekey,base64.b64decode(b64_encrypted_key))
        
        have_derived_key = False

        if rest_params["keys"] == "ukey":
            self.server.add_U(decrypted_key)
            self.server.auth_tag = json_body['auth_tag']
            self.server.payload = json_body.get('payload',None)
            
            have_derived_key = self.server.attempt_decryption(self)
        elif rest_params["keys"] == "vkey":
            self.server.add_V(decrypted_key)
            have_derived_key = self.server.attempt_decryption(self)
        else:
            logger.warning('POST returning  response. uri not supported: ' + self.path)
            common.echo_json_response(self, 400, "uri not supported")
            return
        logger.info('POST of %s key returning 200'%(('V','U')[rest_params["keys"] == "ukey"]))
        common.echo_json_response(self, 200, "Success")
        
        # no key yet, then we're done
        if not have_derived_key:
            return
        
        # woo hoo we have a key 
        # ok lets write out the key now
        secdir = secure_mount.mount() # confirm that storage is still securely mounted
        
        # clean out the secure dir of any previous info before we extract files
        if os.path.isdir("%s/unzipped"%secdir):
            shutil.rmtree("%s/unzipped"%secdir)
        
        # write out key file
        f = open(secdir+"/"+self.server.enc_keyname,'w')
        f.write(base64.b64encode(self.server.K))
        f.close()
        
        #stow the U value for later
        tpm_nvram.write_key_nvram(self.server.final_U)
        
        # optionally extend a hash of they key and payload into specified PCR
        tomeasure = self.server.K
        
        # if we have a good key, now attempt to write out the encrypted payload
        dec_path = "%s/%s"%(secdir, config.get('cloud_node',"dec_payload_file"))
        enc_path = "%s/encrypted_payload"%common.WORK_DIR
        
        dec_payload = None
        enc_payload = None
        
        if self.server.payload is not None:
            dec_payload = crypto.decrypt(self.server.payload, str(self.server.K))
            enc_payload = self.server.payload
        elif os.path.exists(enc_path):
            # if no payload provided, try to decrypt one from a previous run stored in encrypted_payload
            with open(enc_path,'r') as f:
                enc_payload = f.read()
            try:
                dec_payload = crypto.decrypt(enc_payload,str(self.server.K))
                logger.info("Decrypted previous payload in %s to %s"%(enc_path,dec_path))
            except Exception as e:
                logger.warning("Unable to decrypt previous payload %s with derived key: %s"%(enc_path,e))
                os.remove(enc_path)
                enc_payload=None
        
        # also write out encrypted payload to be decrytped next time
        if enc_payload is not None:
            with open(enc_path,'w') as f:
                f.write(self.server.payload)

        # deal with payload
        payload_thread = None
        if dec_payload is not None:
            tomeasure += dec_payload
            # see if payload is a zip
            zfio = cStringIO.StringIO(dec_payload)
            if config.getboolean('cloud_node','extract_payload_zip') and zipfile.is_zipfile(zfio):
                logger.info("Decrypting and unzipping payload to %s/unzipped"%secdir)
                with zipfile.ZipFile(zfio,'r')as f:
                    f.extractall('%s/unzipped'%secdir)
                
                # run an included script if one has been provided
                initscript = config.get('cloud_node','payload_script')
                if initscript is not "":
                    def initthread():
                        import subprocess
                        logger.debug("Executing specified script: %s"%initscript)
                        env = os.environ.copy()
                        env['NODE_UUID']=self.server.node_uuid
                        env["LD_LIBRARY_PATH"] = "/newlib:" + env["LD_LIBRARY_PATH"]
                        proc= subprocess.Popen(["/bin/sh",initscript],env=env,shell=False,cwd='%s/unzipped'%secdir,
                                                stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                        proc.wait()
                        while True:
                            line = proc.stdout.readline()
                            if line=="":
                                break
                            logger.debug("init-output: %s"%line.strip())
                            
                    payload_thread = threading.Thread(target=initthread)
            else:
                logger.info("Decrypting payload to %s"%dec_path)
                with open(dec_path,'w') as f:
                    f.write(dec_payload)
            zfio.close()

        # now extend a measurement of the payload and key if there was one
        pcr = config.getint('cloud_node','measure_payload_pcr')
        if pcr>0 and pcr<24:
            logger.info("extending measurement of payload into PCR %s"%pcr)
            measured = hashlib.sha1(tomeasure).hexdigest()
            tpm_exec.run("extend -ix %d -ih %s"%(pcr,measured))
            
        if payload_thread is not None:
            payload_thread.start()
            
        return

    def get_query_tag_value(self, path, query_tag):
        """This is a utility method to query for specific the http parameters in the uri.  
        
        Returns the value of the parameter, or None if not found."""  
        data = { }
        parsed_path = urlparse(self.path)
        query_tokens = parsed_path.query.split('&')
        # find the 'ids' query, there can only be one
        for tok in query_tokens:
            query_tok = tok.split('=')
            query_key = query_tok[0]
            if query_key is not None and query_key == query_tag:
                # ids tag contains a comma delimited list of ids
                data[query_tag] = query_tok[1]    
                break        
        return data.get(query_tag,None) 
    
    def log_message(self, logformat, *args):
        return
                
#consider using PooledProcessMixIn
# https://github.com/muayyad-alsadi/python-PooledProcessMixIn
class CloudNodeHTTPServer(ThreadingMixIn, HTTPServer):
    """Http Server which will handle each request in a separate thread."""
   
    ''' Do not modify directly unless you acquire uvLock. Set chosen for uniqueness of contained values''' 
    u_set = set([])
    v_set = set([])
    
    rsaprivatekey = None
    rsapublickey = None
    rsapublickey_exportable = None
    done = threading.Event()
    auth_tag = None
    payload = None
    enc_keyname = None
    K = None
    final_U = None
    node_uuid = None
    
    def __init__(self, server_address, RequestHandlerClass, node_uuid):
        """Constructor overridden to provide ability to pass configuration arguments to the server"""
        secdir = secure_mount.mount()
        keyname = "%s/%s"%(secdir,config.get('cloud_node','rsa_keyname'))
        
        # read or generate the key depending on configuration
        if os.path.isfile(keyname):
            # read in private key
            logger.debug( "Using existing key in %s"%keyname)
            f = open(keyname,"r")
            rsa_key = crypto.rsa_import_privkey(f.read())
        else:
            logger.debug("key not found, generating a new one")
            rsa_key = crypto.rsa_generate(2048)
            with open(keyname,"w") as f:
                f.write(crypto.rsa_export_privkey(rsa_key))
        
        self.rsaprivatekey = rsa_key
        self.rsapublickey_exportable = crypto.rsa_export_pubkey(self.rsaprivatekey)
        
        #attempt to get a U value from the TPM NVRAM
        nvram_u = tpm_nvram.read_key_nvram()
        if nvram_u is not None:
            logger.info("Existing U loaded from TPM NVRAM")
            self.add_U(nvram_u)
        BaseHTTPServer.HTTPServer.__init__(self, server_address, RequestHandlerClass)
        self.enc_keyname = config.get('cloud_node','enc_keyname')
        self.node_uuid = node_uuid


    def add_U(self, u):
        """Threadsafe method for adding a U value received from the Tenant
        
        Do not modify u_set of v_set directly.
        """
        with uvLock:
            # be very careful printing K, U, or V as they leak in logs stored on unprotected disks
            if common.DEVELOP_IN_ECLIPSE:
                logger.debug( "Adding U len %d data:%s"%(len(u),base64.b64encode(u)))
            self.u_set.add(u)

        
    def add_V(self, v):
        """Threadsafe method for adding a U value received from the Cloud Verifier
        
        Do not modify u_set of v_set directly.        
        """
        with uvLock:
            # be very careful printing K, U, or V as they leak in logs stored on unprotected disks
            if common.DEVELOP_IN_ECLIPSE:
                logger.debug( "Adding V: " + base64.b64encode(v))
            self.v_set.add(v)

    def attempt_decryption(self, handler):
        """On reception of a U or V value, this method is called to attempt the decryption of the Cloud Init script
        
        At least one U and V value must be received in order to attempt encryption. Multiple U and V values are stored
        to prevent an attacker from sending U/V values to deny service.  
        """
        with uvLock:
            both_u_and_v_present = False
            return_value = False
            for u in self.u_set:
                for v in self.v_set:
                    both_u_and_v_present = True
                    return_value = self.decrypt_check(u,v)
                    if return_value:
                        # reset u and v sets
                        self.u_set= set([])
                        self.v_set= set([])
                        return return_value
            #TODO check on whether this happens or not.  NVRAM causes trouble
            if both_u_and_v_present: 
                pass
                #logger.critical("Possible attack from: " + str(handler.client_address) + ".  Both U (potentially stale from TPM NVRAM) and V present but unsuccessful in attempt to decrypt check value.")
            return return_value
            
    def decrypt_check(self, decrypted_U, decrypted_V):    
        """Decrypt the Cloud init script with the passed U and V values.
        
        This method will access the received auth tag, and may fail if decoy U and V values were received.
        Do not call directly unless you acquire uvLock. Returns None if decryption unsuccessful, else returns the 
        decrypted node UUID.
        """
        if self.auth_tag is None:
            return None
        
        if len(decrypted_U) != len(decrypted_V):
            logger.warning("Invalid U len %d or V len %d. skipping..."%(len(decrypted_U),len(decrypted_V)))
            return None
        
        candidate_key = str(crypto.strbitxor(decrypted_U, decrypted_V))
        
        # be very careful printing K, U, or V as they leak in logs stored on unprotected disks
        if common.DEVELOP_IN_ECLIPSE:
            logger.debug("U: " + base64.b64encode(decrypted_U))
            logger.debug("V: " + base64.b64encode(decrypted_V))
            logger.debug("K: " + base64.b64encode(candidate_key))
            
        logger.debug( "auth_tag: " + self.auth_tag)
        ex_mac = crypto.do_hmac(candidate_key,self.node_uuid)
        
        if ex_mac == self.auth_tag:
            logger.info( "Successfully derived K for UUID %s",self.node_uuid)
            self.final_U = decrypted_U
            self.K = candidate_key
            return True

        return False
                        
def main(argv=sys.argv):
    if os.getuid()!=0 and not common.DEVELOP_IN_ECLIPSE:
        logger.critical("This process must be run as root.")
        return

    # get params for initialization
    registrar_ip = config.get('general', 'registrar_ip')
    registrar_port = config.get('general', 'registrar_port')
    
    # initialize the tmpfs partition to store keys if it isn't already available
    secdir = secure_mount.mount()

    # change dir to working dir
    common.ch_dir(common.WORK_DIR,logger)
    
    #initialize tpm 
    (ek,ekcert,aik) = tpm_initialize.init(self_activate=False,config_pw=config.get('cloud_node','tpm_ownerpassword')) # this tells initialize not to self activate the AIK
    virtual_node = tpm_initialize.is_vtpm()
    
    # try to get some TPM randomness into the system entropy pool
    tpm_random.init_system_rand()
    
    if common.STUB_TPM:
        ekcert = common.TEST_EK_CERT
        
    if virtual_node and (ekcert is None or common.STUB_TPM):
        ekcert = 'virtual'
        
    # now we need the UUID
    try:
        node_uuid = config.get('cloud_node','node_uuid')
    except ConfigParser.NoOptionError:
        node_uuid = None
    if node_uuid == 'openstack':
        node_uuid = openstack.get_openstack_uuid()
    elif node_uuid == 'hash_ek':
        node_uuid = hashlib.sha256(ek).hexdigest()
    elif node_uuid == 'generate' or node_uuid is None:
        node_uuid = str(uuid.uuid4())
    if common.DEVELOP_IN_ECLIPSE:
        node_uuid = "C432FBB3-D2F1-4A97-9EF7-75BD81C866E9"
        
    # register it and get back a blob
    keyblob = registrar_client.doRegisterNode(registrar_ip,registrar_port,node_uuid,ek,ekcert,aik)
    
    if keyblob is None:
        raise Exception("Registration failed")
    
    # get the ephemeral registrar key
    key = tpm_initialize.activate_identity(keyblob)
    
    # tell the registrar server we know the key
    if virtual_node:
        deepquote = tpm_quote.create_deep_quote(hashlib.sha1(key).hexdigest(),node_uuid+aik+ek)
        registrar_client.doActivateVirtualNode(registrar_ip, registrar_port, node_uuid, deepquote)
    else:
        registrar_client.doActivateNode(registrar_ip,registrar_port,node_uuid,key)

    serveraddr = ('', config.getint('general', 'cloudnode_port'))
    server = CloudNodeHTTPServer(serveraddr,Handler,node_uuid)
    serverthread = threading.Thread(target=server.serve_forever)

    logger.info( 'Starting Cloud Node on port %s use <Ctrl-C> to stop'%serveraddr[1])
    serverthread.start()
    
    # want to listen for revocations?
    if config.getboolean('cloud_node','listen_notfications'):
        cert_path = config.get('cloud_node','revocation_cert')
        if cert_path == "default":
            cert_path = '%s/unzipped/RevocationNotifier-cert.crt'%(secdir)
        elif cert_path[0]!='/':
            # if it is a relative, convert to absolute in work_dir
            cert_path = os.path.abspath('%s/%s'%(common.WORK_DIR,cert_path))
            
        def perform_actions(revocation):
            actionlist = config.get('cloud_node','revocation_actions')
            if actionlist.strip() == "":
                logger.debug("No revocation actions specified")
                return
            if actionlist =='default':
                # load actions from unzipped
                with open("%s/unzipped/action_list"%secdir,'r') as f:
                    actionlist = f.read()
 
                actionlist = actionlist.strip().split(',')
                uzpath = "%s/unzipped"%secdir
                if uzpath not in sys.path:
                    sys.path.append(uzpath)
            else:
                # load the actions from inside the keylime module
                actionlist = actionlist.split(',')
                actionlist = ["revocation_actions.%s"%i for i in actionlist]
            
            for action in actionlist:
                module = importlib.import_module(action)
                execute = getattr(module,'execute')
                try:
                    execute(revocation)
                except Exception as e:
                    logger.warn("Exception during exeuction of revocation action %s: %s"%(action,e))
        try:
            while True:
                try:
                    revocation_notifier.await_notifications(perform_actions,revocation_cert_path=cert_path)
                except Exception as e:
                    logger.exception(e)
                    logger.warn("No connection to revocation server, retrying in 10s...")
                    time.sleep(10)
        except KeyboardInterrupt:
            logger.info("TERM Signal received, shutting down...")
            tpm_initialize.flush_keys()
            server.shutdown()
    else:  
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("TERM Signal received, shutting down...")
            tpm_initialize.flush_keys()
            server.shutdown()

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        logger.exception(e)

