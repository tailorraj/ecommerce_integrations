"""
Shopify Product Metafields Integration
Fetch and sync Shopify metafields to ERPNext custom fields
"""

import frappe
from frappe import _
import requests
import json
from typing import Optional, Dict, Any

from ecommerce_integrations.shopify.constants import API_VERSION, SETTING_DOCTYPE


# Mapping between Shopify metafield keys and ERPNext custom field names
METAFIELD_MAPPING = {
	"reference": "custom_reference",
	"collection": "custom_collection",
	"dial_size": "custom_dial_size",
	"dial_shape": "custom_dial_shape",
	"case_material": "custom_case_material",
	"diamonds": "custom_diamonds",
	"strap_bracelet": "custom_strapbracelet",
	"gender": "custom_gender",
	"movement": "custom_movement",
	"water_resistance": "custom_water_resistant",
	"warranty": "custom_brand_warranty",
}


def get_graphql_query(product_id: str) -> str:
	"""
	Generate GraphQL query to fetch product metafields from Shopify.
	
	Args:
		product_id: Shopify product ID (numeric)
	
	Returns:
		GraphQL query string
	"""
	gid = f"gid://shopify/Product/{product_id}"
	
	query = """
	{
		product(id: "%s") {
			id
			title
			metafield_reference: metafield(namespace: "accentuate", key: "reference") {
				key
				value
			}
			metafield_collection: metafield(namespace: "accentuate", key: "collection") {
				key
				value
			}
			metafield_dial_size: metafield(namespace: "accentuate", key: "dial_size") {
				key
				value
			}
			metafield_dial_shape: metafield(namespace: "accentuate", key: "dial_shape") {
				key
				value
			}
			metafield_case_material: metafield(namespace: "accentuate", key: "case_material") {
				key
				value
			}
			metafield_diamonds: metafield(namespace: "accentuate", key: "diamonds") {
				key
				value
			}
			metafield_strap_bracelet: metafield(namespace: "accentuate", key: "strap_bracelet") {
				key
				value
			}
			metafield_gender: metafield(namespace: "accentuate", key: "gender") {
				key
				value
			}
			metafield_movement: metafield(namespace: "accentuate", key: "movement") {
				key
				value
			}
			metafield_water_resistant: metafield(namespace: "accentuate", key: "water_resistance") {
				key
				value
			}
			metafield_warranty: metafield(namespace: "accentuate", key: "warranty") {
				key
				value
			}
		}
	}
	""" % gid
	
	return query


def fetch_product_metafields(product_id: str) -> Optional[Dict[str, Any]]:
	"""
	Fetch product metafields from Shopify using GraphQL API.
	
	Args:
		product_id: Shopify product ID (numeric string or int)
	
	Returns:
		Dictionary containing metafield data or None if request fails
	"""
	try:
		# Get Shopify settings
		setting = frappe.get_doc(SETTING_DOCTYPE)
		
		if not setting.is_enabled():
			frappe.log_error(
				message="Shopify integration is not enabled",
				title="Metafield Fetch Error"
			)
			return None
		
		# Prepare GraphQL request
		shopify_url = setting.shopify_url
		password = setting.get_password("password")
		
		# Construct GraphQL endpoint
		graphql_url = f"https://{shopify_url}/admin/api/{API_VERSION}/graphql.json"
		
		# Prepare headers
		headers = {
			"Content-Type": "application/json",
			"X-Shopify-Access-Token": password
		}
		
		# Prepare payload
		query = get_graphql_query(str(product_id))
		payload = {"query": query}
		
		# Make request
		response = requests.post(
			graphql_url,
			headers=headers,
			data=json.dumps(payload),
			timeout=30
		)
		
		if response.status_code == 200:
			data = response.json()
			
			# Check for GraphQL errors
			if "errors" in data:
				frappe.log_error(
					message=f"GraphQL Errors: {json.dumps(data['errors'])}",
					title="Shopify Metafield GraphQL Error"
				)
				return None
			
			return data.get("data", {}).get("product")
		else:
			frappe.log_error(
				message=f"Status: {response.status_code}, Response: {response.text}",
				title="Shopify Metafield API Error"
			)
			return None
			
	except Exception as e:
		frappe.log_error(
			message=frappe.get_traceback(),
			title="Shopify Metafield Fetch Exception"
		)
		return None


def extract_metafield_values(product_data: Dict[str, Any]) -> Dict[str, str]:
	"""
	Extract metafield values from Shopify product data.
	
	Args:
		product_data: Product data from Shopify GraphQL response
	
	Returns:
		Dictionary mapping ERPNext custom field names to values
	"""
	metafield_values = {}
	
	if not product_data:
		return metafield_values
	
	# Map Shopify metafield keys to ERPNext custom fields
	metafield_keys = {
		"metafield_reference": "custom_reference",
		"metafield_collection": "custom_collection",
		"metafield_dial_size": "custom_dial_size",
		"metafield_dial_shape": "custom_dial_shape",
		"metafield_case_material": "custom_case_material",
		"metafield_diamonds": "custom_diamonds",
		"metafield_strap_bracelet": "custom_strapbracelet",
		"metafield_gender": "custom_gender",
		"metafield_movement": "custom_movement",
		"metafield_water_resistant": "custom_water_resistant",
		"metafield_warranty": "custom_brand_warranty",
	}
	
	for shopify_key, erpnext_field in metafield_keys.items():
		metafield = product_data.get(shopify_key)
		if metafield and metafield.get("value"):
			metafield_values[erpnext_field] = metafield.get("value")
	
	return metafield_values


def add_metafields_to_item_dict(item_dict: Dict[str, Any], product_id: str) -> Dict[str, Any]:
	"""
	Fetch metafields from Shopify and add them to the item dictionary.
	This function should be called before creating/updating an item in ERPNext.
	
	Args:
		item_dict: Dictionary containing item data to be created/updated in ERPNext
		product_id: Shopify product ID
	
	Returns:
		Updated item_dict with metafield values
	"""
	try:
		# Fetch metafields from Shopify
		product_data = fetch_product_metafields(product_id)
		
		if not product_data:
			# If metafield fetch fails, return original dict without modifications
			return item_dict
		
		# Extract metafield values
		metafield_values = extract_metafield_values(product_data)
		
		# Add metafield values to item_dict
		for field_name, field_value in metafield_values.items():
			if field_value:  # Only add non-empty values
				item_dict[field_name] = field_value
		
		frappe.logger().debug(
			f"Added {len(metafield_values)} metafields to item dict for product {product_id}"
		)
		
	except Exception as e:
		# Log error but don't fail the item creation
		frappe.log_error(
			message=f"Failed to add metafields for product {product_id}: {str(e)}\n{frappe.get_traceback()}",
			title="Metafield Addition Error"
		)
	
	return item_dict


def update_item_metafields(item_name: str, product_id: str) -> bool:
	"""
	Update metafields for an existing ERPNext item.
	
	Args:
		item_name: ERPNext Item name
		product_id: Shopify product ID
	
	Returns:
		True if update was successful, False otherwise
	"""
	try:
		# Fetch metafields from Shopify
		product_data = fetch_product_metafields(product_id)
		
		if not product_data:
			return False
		
		# Extract metafield values
		metafield_values = extract_metafield_values(product_data)
		
		if not metafield_values:
			frappe.logger().debug(f"No metafields found for product {product_id}")
			return False
		
		# Get the item document
		item = frappe.get_doc("Item", item_name)
		
		# Update metafield values
		for field_name, field_value in metafield_values.items():
			if field_value and hasattr(item, field_name):
				setattr(item, field_name, field_value)
		
		# Save the item
		item.flags.ignore_hooks = True  # Avoid triggering upload hooks
		item.save()
		
		frappe.logger().info(
			f"Updated {len(metafield_values)} metafields for item {item_name}"
		)
		
		return True
		
	except Exception as e:
		frappe.log_error(
			message=f"Failed to update metafields for item {item_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Metafield Update Error"
		)
		return False
