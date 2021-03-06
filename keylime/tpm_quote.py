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

import sys
import base64
import tpm_exec
import tpm_cexec
import tempfile
import hashlib
import os
import tpm_initialize
import common
import traceback
import sets
import time
import ima
import json

logger = common.init_logging('tpm_quote')

EMPTYMASK="1"
EMPTY_PCR="0000000000000000000000000000000000000000"

def check_mask(mask,pcr):
    if mask is None:
        return False
    return bool(1<<pcr & int(mask,0))

def create_deep_quote(nonce,data=None,vpcrmask=EMPTYMASK,pcrmask=EMPTYMASK):
    # don't deep quote when developing
    if common.DEVELOP_IN_ECLIPSE or common.STUB_TPM:
    # if not using TPM, just return a canned quote
        time.sleep(common.TEST_CREATE_DEEP_QUOTE_DELAY)
        return common.TEST_DQ
    
    quotepath = None
    try:
        # read in the vTPM key handle
        keyhandle = tpm_initialize.load_aik()
        owner_pw = tpm_initialize.get_tpm_metadata('owner_pw')

        if pcrmask is None:
            pcrmask = EMPTYMASK
        if vpcrmask is None:
            vpcrmask = EMPTYMASK
            
        # need to hold the lock while we reset and extend the pcr and then do the quote
        with tpm_exec.tpmutilLock:
            if data is not None:
                # add PCR 16 to pcrmask
                pcrmask = "0x%X"%(int(pcrmask,0) + (1 << common.TPM_DATA_PCR))
                tpm_exec.run("pcrreset -ix %d"%common.TPM_DATA_PCR,lock=False)
                tpm_exec.run("extend -ix %d -ic %s"%(common.TPM_DATA_PCR,hashlib.sha1(data).hexdigest()),lock=False)
            
            #make a temp file for the quote 
            quotefd,quotepath = tempfile.mkstemp()
            
            command = "deepquote -vk %s -hm %s -vm %s -nonce %s -pwdo %s -oq %s" % (keyhandle, pcrmask, vpcrmask, nonce, owner_pw,quotepath)
            #print("Executing %s"%(command))
            tpm_exec.run(command,lock=False)

        # read in the quote
        f = open(quotepath,"rb")
        quote = base64.b64encode(f.read().encode("zlib"))
        f.close()
        os.close(quotefd)
    finally:
        if quotepath is not None:
            os.remove(quotepath)

    return 'd'+quote

def create_quote(nonce,data=None,pcrmask=EMPTYMASK):
    # if not using TPM, just return a canned quote
    if common.STUB_TPM:
        time.sleep(common.TEST_CREATE_QUOTE_DELAY)
        return common.TEST_QUOTE
        
    quotepath = None
    try:
        keyhandle = tpm_initialize.load_aik()
        if pcrmask is None:
            pcrmask = EMPTYMASK

        with tpm_exec.tpmutilLock:
            if data is not None:
                # add PCR 16 to pcrmask
                pcrmask = "0x%X"%(int(pcrmask,0) + (1 << common.TPM_DATA_PCR))
                tpm_exec.run("pcrreset -ix %d"%common.TPM_DATA_PCR,lock=False)
                tpm_exec.run("extend -ix %d -ic %s"%(common.TPM_DATA_PCR,hashlib.sha1(data).hexdigest()),lock=False)
            
            #make a temp file for the quote 
            quotefd,quotepath = tempfile.mkstemp()
            tpm_exec.run("tpmquote -hk %s -bm %s -nonce %s -noverify -oq %s"%(keyhandle,pcrmask,nonce,quotepath),lock=False)
            
        # read in the quote
        f = open(quotepath,"rb")
        quote = base64.b64encode(f.read().encode("zlib"))
        f.close()
        os.close(quotefd)
    finally:
        if quotepath is not None:
            os.remove(quotepath)

    return 'r'+quote

def is_deep_quote(quote):
    if quote[0]=='d':
        return True
    elif quote[0]=='r':
        return False
    else:
        raise Exception("Invalid quote type %s"%quote[0])

def check_deep_quote(nonce,data,quote,vAIK,hAIK,vtpm_policy={},tpm_policy={},ima_measurement_list=None,ima_whitelist={}):
    quoteFile=None
    vAIKFile=None
    hAIKFile=None

    if common.STUB_TPM:
        nonce = common.TEST_DQ_NONCE
        vAIK=common.TEST_VAIK

    
    if common.PRINT_DEEPQUOTE_INFO:
        print "TEST_DQ='%s'"%quote
        print "TEST_DQ_NONCE='%s'"%nonce
        #magic numbers get rid of the extra quotes
        print "TEST_HAIK='%s'"%repr(str(hAIK))[1:-1]
        print "TEST_VAIK='%s'"%repr(str(vAIK))[1:-1]
        
    if quote[0]!='d':
        raise Exception("Invalid deep quote type %s"%quote[0])
    quote = quote[1:]
    
    try:
        # write out quote
        qfd, qtemp = tempfile.mkstemp()
        quoteFile = open(qtemp,"wb")
        quoteFile.write(base64.b64decode(quote).decode("zlib"))
        quoteFile.close()
        os.close(qfd)

        afd, atemp = tempfile.mkstemp()
        vAIKFile = open(atemp,"w")
        vAIKFile.write(vAIK)
        vAIKFile.close()
        os.close(afd)
        
        afd, atemp = tempfile.mkstemp()
        hAIKFile = open(atemp,"w")
        hAIKFile.write(hAIK)
        hAIKFile.close()
        os.close(afd)


        retout = tpm_cexec.checkdeepquote(hAIKFile.name, vAIKFile.name, quoteFile.name, nonce)
    except Exception as e:
        logger.error("Error verifying quote: %s"%(e))
        logger.error(traceback.format_exc())
        return False
    finally:
        if vAIKFile is not None:
            os.remove(vAIKFile.name)
        if hAIKFile is not None:
            os.remove(hAIKFile.name)
        if quoteFile is not None:
            os.remove(quoteFile.name)
        pass
    
    if len(retout)<1:
        return False

    if retout[0]!="Verification against AIK succeeded\n":
        logger.error("Failed to validate signature, output: %s"%retout)
        return False
    
    pcrs = None
    vpcrs = None
    for line in retout:
        if line=="PCR contents from quote:\n":
            pcrs = []
            continue
        if line=="PCR contents from vTPM quote:\n":
            vpcrs = []
            continue
        if line=="\n":
            continue
        # order important here
        if vpcrs is not None:
            vpcrs.append(line)
        elif pcrs is not None:
            pcrs.append(line)
    
    # don't pass in data to check pcrs for physical quote 
    return check_pcrs(tpm_policy,pcrs,None,False,None,None) and check_pcrs(vtpm_policy, vpcrs, data, True,ima_measurement_list,ima_whitelist)

def check_quote(nonce,data,quote,aikFromRegistrar,tpm_policy={},ima_measurement_list=None,ima_whitelist={}):
    quoteFile=None
    aikFile=None

    if common.STUB_TPM:
        nonce = common.TEST_NONCE
    
    if common.PRINT_QUOTE_INFO:
        print "TEST_QUOTE='%s'"%quote
        print "TEST_NONCE='%s'"%nonce
        #magic numbers get rid of the extra quotes
        print "TEST_AIK='%s'"%repr(str(aikFromRegistrar))[1:-1]

    if quote[0]!='r':
        raise Exception("Invalid quote type %s"%quote[0])
    quote = quote[1:]
    
    try:
        # write out quote
        qfd, qtemp = tempfile.mkstemp()
        quoteFile = open(qtemp,"wb")

        quoteFile.write(base64.b64decode(quote).decode("zlib"))
        quoteFile.close()
        os.close(qfd)

        afd, atemp = tempfile.mkstemp()
        aikFile = open(atemp,"w")
        aikFile.write(aikFromRegistrar)
        aikFile.close()
        os.close(afd)

        retout = tpm_cexec.check_quote(aikFile.name, quoteFile.name, nonce)
    except Exception as e:
        logger.error("Error verifying quote: "+str(e))
        logger.error(traceback.format_exc())
        return False
    finally:
        if aikFile is not None:
            os.remove(aikFile.name)
        if quoteFile is not None:
            os.remove(quoteFile.name)
        pass

    if len(retout)<1:
        return False

    if retout[0]!="Verification against AIK succeeded\n":
        logger.error("Failed to validate signature, output: %s"%retout)
        return False
    
    pcrs = None
    for line in retout[1:]:
        if line=="PCR contents from quote:\n":
            pcrs = []
            continue
        if line=="\n":
            continue
        if pcrs is not None:
            pcrs.append(line)    

    return check_pcrs(tpm_policy,pcrs,data,False,ima_measurement_list,ima_whitelist)

def check_pcrs(tpm_policy,pcrs,data,virtual,ima_measurement_list,ima_whitelist):
    pcrWhiteList = tpm_policy.copy()
    if 'mask' in pcrWhiteList: del pcrWhiteList['mask']
    # convert all pcr num keys to integers
    pcrWhiteList = {int(k):v for k,v in pcrWhiteList.items()}
    
    pcrsInQuote=sets.Set()
    for line in pcrs:
        tokens = line.split()
        if len(tokens)<3:
            logger.error("Invalid %sPCR in quote: %s"%(("","v")[virtual],pcrs))
            continue
        
        # always lower case
        pcrval = tokens[2].lower()
        # convert pcr num to number
        try:
            pcrnum = int(tokens[1])
        except Exception:
            logger.error("Invalide PCR number %s"%tokens[1])
        
        if pcrnum==common.TPM_DATA_PCR and data is not None:
            # compute expected value  H(0|H(string(H(data))))
            # confused yet?  pcrextend will hash the string of the original hash again
            expectedval = hashlib.sha1(EMPTY_PCR.decode('hex')+hashlib.sha1(hashlib.sha1(data).hexdigest()).digest()).hexdigest().lower()
            if expectedval != pcrval and not common.STUB_TPM:
                logger.error("%sPCR #%s: invalid bind data %s from quote does not match expected value %s"%(("","v")[virtual],pcrnum,pcrval,expectedval))
                return False
            continue
               
        # check for ima PCR
        if pcrnum==common.IMA_PCR and not common.STUB_TPM:
            if ima_measurement_list==None:
                logger.error("IMA PCR in policy, but no measurement list provided")
                return False
            
            if check_ima(pcrval,ima_measurement_list,ima_whitelist):
                pcrsInQuote.add(pcrnum)
                continue
            else:
                return False
                
        if pcrnum not in pcrWhiteList.keys():
            if not common.STUB_TPM and len(tpm_policy.keys())>0:
                logger.warn("%sPCR #%s in quote not found in tpm_policy, skipping."%(("","v")[virtual],pcrnum))
            continue
        elif pcrval not in pcrWhiteList[pcrnum] and not common.STUB_TPM:
            logger.error("%sPCR #%s: %s from quote does not match expected value %s"%(("","v")[virtual],pcrnum,pcrval,pcrWhiteList[pcrnum]))
            return False
        else:
            pcrsInQuote.add(pcrnum)       

    if common.STUB_TPM:
        return True

    missing = list(sets.Set(pcrWhiteList.keys()).difference(pcrsInQuote))
    if len(missing)>0:
        logger.error("%sPCRs specified in policy not in quote: %s"%(("","v")[virtual],missing))
        return False
    return True

def check_ima(pcrval,ima_measurement_list,ima_whitelist):
    logger.info("Checking IMA measurement list...")
    ex_value = ima.process_measurement_list(ima_measurement_list.split('\n'),ima_whitelist)
    if ex_value is None:
        return False
    
    if pcrval != ex_value and not common.DEVELOP_IN_ECLIPSE:
        logger.error("IMA measurement list expected pcr value %s does not match TPM PCR %s"%(ex_value,pcrval))
        return False
    logger.debug("IMA measurement list validated")
    return True

def readPolicy(configval):
    policy = json.loads(configval)
    
    # compute PCR mask from tpm_policy
    mask = 0
    for key in policy.keys():
        if not key.isdigit() or int(key)>24:
            raise Exception("Invalid tpm policy pcr number: %s"%(key))
        
        if int(key)==common.TPM_DATA_PCR:
            raise Exception("Invalid whitelist PCR number %s, keylime uses this PCR to bind data."%key)
        if int(key)==common.IMA_PCR:
            raise Exception("Invalid whitelist PCR number %s, this PCR is used for IMA."%key)
        
        mask = mask + (1<<int(key))
        
        # wrap it in a list if it is a singleton
        if isinstance(policy[key],basestring):
            policy[key]=[policy[key]]
         
        # convert all hash values to lowercase
        policy[key] = [x.lower() for x in policy[key]]
    
    policy['mask'] = "0x%X"%(mask)
    return policy

# this is just for testing
def main(argv=sys.argv):
    nonce = "NABIL"
    rsa_key = "somebase64data"
    
    print "creating quote..."
    quote = create_quote(nonce,rsa_key)
    print "\tDONE"

    print "checking quote..."
    if common.STUB_TPM:
        aik = common.TEST_AIK
    else:
        aik = tpm_initialize.get_tpm_metadata('aik')
        
    print "\tVerified %s"%check_quote(nonce,rsa_key,quote,aik)

    print "creating full quote..."
    # this is a quote for pcr 22,2
    quote = create_quote(nonce,rsa_key,"0x400004")
    print "\tDONE"

    print "checking full quote..."
    
    json_tpm_policy = '{"22":"ffffffffffffffffffffffffffffffffffffffff","02":"0000000000000000000000000000000000000000"}'
    
    tpm_policy = readPolicy(json_tpm_policy)
    print "\tVerified %s"%check_quote(nonce,rsa_key,quote,aik,tpm_policy)
    
    print "\n========\n\nchecking deepquote"
    print "\tVerified %s"%check_deep_quote(common.TEST_DQ_NONCE, None, common.TEST_DQ, common.TEST_VAIK, common.TEST_HAIK, {}, {})
    
    if True:
        sys.exit(0)
        
    print "creating a bunch of quotes"
    for _ in range(1000):
        create_quote(nonce,rsa_key)
        check_quote(nonce,rsa_key,quote,aik,tpm_policy)
        pass
    print "done"


if __name__=="__main__":
    try:
        main()
    except Exception as e:
        logger.exception(e)