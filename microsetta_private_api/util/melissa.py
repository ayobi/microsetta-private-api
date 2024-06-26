import json
import requests
import urllib.parse

from microsetta_private_api.repo.transaction import Transaction
from microsetta_private_api.repo.melissa_repo import MelissaRepo
from microsetta_private_api.config_manager import SERVER_CONFIG
from microsetta_private_api.exceptions import RepoException

# The response codes we can treat as deliverable
GOOD_CODES = ["AV25", "AV24", "AV23", "AV22", "AV21"]
# NB: We're adding "AV14" as a good code but ONLY if there are no error codes.
# This code reflects an inability to verify at the highest resolution, but we
# have determined that for certain scenarios like Mail Boxes Etc and similar
# locations, it's appropriate to treat as good.
GOOD_CODES_NO_ERROR = ["AV14"]


def verify_address(
        address_1, address_2=None, address_3=None, city=None, state=None,
        postal=None, country=None, block_po_boxes=True
):
    """
    Required parameters: address_1, postal, country
    Optional parameters: address_2, address_3, city, state, block_po_boxes

    Note - postal and country default to None as you can't have non-default
           arguments after default arguments, and preserving structural order
           makes sense for addresses
    Note 2 - block_po_boxes defaults to True because our only current use for
             Melissa is verifying shipping addresses. If a future use arises
             where PO boxes are acceptable, pass block_po_boxes=False into
             this function
    """

    if address_1 is None or len(address_1) < 1 or postal is None or\
            len(postal) < 1 or country is None or len(country) < 1:
        raise KeyError("Must include address_1, postal, and country fields")

    with Transaction() as t:
        melissa_repo = MelissaRepo(t)

        dupe_status = melissa_repo.check_duplicate(address_1, address_2,
                                                   postal, country)

        if dupe_status is not False:
            # duplicate record - return result with an added field noting dupe
            return_dict = {"address_1": dupe_status["result_address_1"],
                           "address_2": dupe_status['result_address_2'],
                           "address_3": dupe_status['result_address_3'],
                           "city": dupe_status['result_city'],
                           "state": dupe_status['result_state'],
                           "postal": dupe_status['result_postal'],
                           "country": dupe_status['result_country'],
                           "latitude": dupe_status['result_latitude'],
                           "longitude": dupe_status['result_longitude'],
                           "valid": dupe_status['result_good'],
                           "duplicate": True}
            return return_dict
        else:
            record_id = melissa_repo.create_record(address_1, address_2,
                                                   address_3, city, state,
                                                   postal, country)

            if record_id is None:
                raise RepoException("Failed to create record in database.")

            url_params = {"id": SERVER_CONFIG["melissa_license_key"],
                          "opt": "DeliveryLines:ON",
                          "format": "JSON",
                          "t": record_id,
                          "a1": address_1,
                          "loc": city,
                          "admarea": state,
                          "postal": postal,
                          "ctry": country}

            # Melissa API behaves oddly if it receives null values for a2
            # and a3, convert to "" if necessary
            if address_2 is not None:
                url_params["a2"] = address_2
            else:
                url_params["a2"] = ""

            if address_3 is not None:
                url_params["a3"] = address_3
            else:
                url_params["a3"] = ""

            url = SERVER_CONFIG["melissa_url"] + "?%s" % \
                urllib.parse.urlencode(url_params)

            response = requests.get(url)
            if response.ok is False:
                exception_msg = "Error connecting to Melissa API."
                exception_msg += " Status Code: " + response.status_code
                exception_msg += " Status Text: " + response.reason
                raise Exception(exception_msg)

            response_raw = response.text
            response_obj = json.loads(response_raw)
            if "Records" in response_obj.keys():
                """
                Note: Melissa's Global Address API allows batch requests.
                    However, our usage is on a single-record basis. Therefore,
                    we can safely assume that the response will only include
                    one record to parse and use.
                """

                record_obj = response_obj["Records"][0]

                r_formatted_address = record_obj["FormattedAddress"]
                r_codes = record_obj["Results"]
                r_good = False
                r_errors_present = False
                r_good_conditional = False

                codes = r_codes.split(",")
                for code in codes:
                    if code[0:2] == "AE":
                        r_errors_present = True
                    if code in GOOD_CODES_NO_ERROR:
                        r_good_conditional = True
                    if code in GOOD_CODES:
                        r_good = True
                        break

                if r_good_conditional and not r_errors_present:
                    r_good = True

                # We can't ship to PO boxes, so we need to block them even if
                # the address is otherwise valid. We check for the AddressType
                # key, as it's only applicable to US addresses
                if block_po_boxes and "AddressType" in record_obj:
                    if record_obj["AddressType"] == "P":
                        # Mark the record bad
                        r_good = False
                        # Inject a custom error code to indicate why
                        r_codes += ",AEPOBOX"

                r_address_1 = record_obj["AddressLine1"]
                r_address_2 = record_obj["AddressLine2"]
                r_address_3 = record_obj["AddressLine3"]
                r_city = record_obj["Locality"]
                r_state = record_obj["AdministrativeArea"]
                r_postal = record_obj["PostalCode"]
                r_country = record_obj["CountryName"]
                r_latitude = record_obj["Latitude"]
                r_longitude = record_obj["Longitude"]

                u_success = melissa_repo.update_results(record_id, url,
                                                        response_raw, r_codes,
                                                        r_good,
                                                        r_formatted_address,
                                                        r_address_1,
                                                        r_address_2,
                                                        r_address_3, r_city,
                                                        r_state, r_postal,
                                                        r_country, r_latitude,
                                                        r_longitude)
                t.commit()

                if u_success is False:
                    exception_msg = "Failed to update results for Melissa "
                    exception_msg += "Address Query " + record_id
                    raise ValueError(exception_msg)

                return_dict = {"address_1": r_address_1,
                               "address_2": r_address_2,
                               "address_3": r_address_3,
                               "city": r_city,
                               "state": r_state,
                               "postal": r_postal,
                               "country": r_country,
                               "latitude": r_latitude,
                               "longitude": r_longitude,
                               "valid": r_good}

                return return_dict
            else:
                t.commit()
                exception_msg = "Melissa Global Address API failed on "
                exception_msg += record_id

                raise Exception(exception_msg)
